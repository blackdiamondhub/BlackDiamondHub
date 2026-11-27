"""The assistant's tool gateway.

An app granted ``tools`` (the voice assistant) opens a session and lists or
calls tools through the broker (``tools.open``/``tools.list``/``tools.call``).
The hub decides the session's mode, never the caller: a ``voice`` session is
GUEST while a stay is active and DASHBOARD otherwise, re-resolved on every
call. Every list and call is filtered to what that mode may do:

* apps' own tools (``PluginSpec.tools``), where the tool lists the mode and
  the app has a tile in it (apps with no tiles: the tool's modes alone);
* the shortcuts the homepage offers in the mode, as ``<app>__shortcut__<id>``;
* the buttons on the app's current status, as ``<app>__action__<row>__<id>``
  (row "" for tile-wide), run through ``status.run_shortcut``/``run_action``
  so the homepage's own checks apply;
* ``hub__status``, a read-only summary of the homepage.

Only apps in the ``assistant_apps`` hub setting (default: scenes and fireplace)
contribute tools or show up in ``hub__status``, and never an app with
``PluginSpec.assistant=False``; this holds for every consumer, owner too.

With the ``assistant_dashboard_control`` hub setting off, DASHBOARD sessions
only get read-only tools. Each open and call is published, by the hub, as
``assistant/<sid>/open`` and ``assistant/<sid>/call`` (apps may not publish
``assistant/``), for a supervisor to audit.

Apps' tools are listed in their own pool and, once an app has listed them,
always served: a slow or failed refresh serves the last good list (kept for
the life of the process) while it finishes in the background. The waker
calls :func:`warm` shortly after start and every few minutes, so even the
first conversation after a restart finds sleeping apps' tools ready.
"""
import hashlib
import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from bdh_sdk import events as bus
from bdh_sdk.hooks import tool_result, tools_value
from bdh_sdk.settings import app_setting
from bdh_sdk.tools import (
    BAD_SESSION, KINDS, SESSION_SECONDS, VOICE, InvalidArgs, Tool, ToolError, tool_name, validate_args,
)
from django.core import signing
from django.db import close_old_connections

from . import quick, status
from .modes import plugins
from .plugins import DASHBOARD, GUEST, OWNER

log = logging.getLogger(__name__)

SALT = "bdh.assistant.session"
OWNER_KIND = "owner"
TOOLS_TTL = 60
LIST_TIMEOUT = 6.0
# The waker keeps tools warm in the modes a voice session can be in.
WARM_MODES = (GUEST, DASHBOARD)
# Warming may wake sleeping apps, so it waits longer than a listing.
WARM_TIMEOUT = 60.0
CALL_TIMEOUT = 25.0
MAX_RESULT = 16 * 1024
AUDIT_RESULT = 300
MAX_DISPLAY = 64
DASHBOARD_CONTROL = "assistant_dashboard_control"
ASSISTANT_APPS = "assistant_apps"
DEFAULT_APPS = ("scenes", "fireplace")
STATUS_TOOL = "hub__status"

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hub-tool")
# Tools hooks get their own pool so a listing never queues behind homepage
# status/shortcut hooks (or sleeping apps being woken) in status's shared one.
_list_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="hub-tools-list")
_EXECUTORS = {"tools": _list_executor}
# Each app's last good tools per mode, for the life of the process: tool
# declarations rarely change, so a slow or failing refresh (or the cache
# dropping the entry) never takes an app's tools out of a listing.
_known = {}
_known_lock = threading.Lock()


class BadSession(Exception):
    pass


# --- sessions ----------------------------------------------------------------

def session_mode(kind):
    """The mode a ``kind`` session gets right now: the hub's call, not the app's."""
    if kind == OWNER_KIND:
        return OWNER
    return GUEST if status.current_stay() is not None else DASHBOARD


def open_session(app_id, kind, display=None):
    """A new session for ``app_id``: ``{token, sid, mode, expires_in}``."""
    if kind not in KINDS:
        raise ValueError(f"unknown session kind {kind!r}")
    sid = secrets.token_hex(8)
    display = str(display)[:MAX_DISPLAY] if display else None
    token = signing.dumps({"sid": sid, "kind": kind, "app": app_id, "display": display},
                          salt=SALT, compress=True)
    mode = session_mode(kind)
    _publish(sid, "open", {"sid": sid, "mode": mode, "kind": kind, "display": display, "app": app_id,
                           "time": time.time()})
    return {"token": token, "sid": sid, "mode": mode, "expires_in": SESSION_SECONDS}


def load_session(token, app_id):
    """The session ``token`` carries, if it is ours, unexpired and ``app_id``'s."""
    if not isinstance(token, str) or not token:
        raise BadSession("missing session token")
    try:
        session = signing.loads(token, salt=SALT, max_age=SESSION_SECONDS)
    except signing.SignatureExpired:
        raise BadSession("session expired") from None
    except signing.BadSignature:
        raise BadSession("session token is invalid") from None
    if not isinstance(session, dict) or session.get("kind") not in KINDS:
        raise BadSession("session token is invalid")
    if session.get("app") != app_id:
        raise BadSession("session belongs to another app")
    return session


# --- the catalog -------------------------------------------------------------

def dashboard_control():
    return app_setting("hub", DASHBOARD_CONTROL, True) is not False


def opted_in(spec):
    return getattr(spec, "assistant", True) is not False


def chosen_apps():
    """The app ids the admin lets the assistant use (saved, else the default)."""
    value = app_setting("hub", ASSISTANT_APPS, None)
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    if not isinstance(value, (list, tuple)):
        value = DEFAULT_APPS
    return {v for v in value if isinstance(v, str) and v}


def usable_specs():
    """The installed apps the assistant may use: allowlisted and not opted out."""
    chosen = chosen_apps()
    return [s for s in plugins() if s.id in chosen and opted_in(s)]


def _tools_value(result):
    """An app's tools hook result as ``tool_dict``s, bad ones dropped."""
    if status._converted(result):
        tools = []
        for raw in result.value or ():
            try:
                tools.append(Tool(**{**raw, "modes": frozenset(raw.get("modes") or ())}))
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("skipping bad tool %r: %s", raw, exc)
        return tools_value(tools)
    return tools_value(result)


def _tools_job(spec, mode):
    return ("tools", spec.id, spec.tools, _tools_value, TOOLS_TTL,
            mode if status._takes_mode(spec.tools) else None)


def _known_tools(kind, plugin_id, mode):
    if kind != "tools":
        return None
    with _known_lock:
        return _known.get((plugin_id, mode))


def _remember(results, jobs):
    """Keep every app's tools that ``results`` has, for :func:`_known_tools`."""
    with _known_lock:
        for kind, plugin_id, _path, _convert, _ttl, mode in jobs:
            entry = results.get((kind, plugin_id))
            if kind == "tools" and entry and not entry.get("missing") and "updated" in entry:
                known = _known.get((plugin_id, mode))
                if known is None or entry["updated"] >= known["updated"]:
                    _known[(plugin_id, mode)] = {"value": entry["value"], "updated": entry["updated"]}


def forget():
    """Drop the remembered tools (tests)."""
    with _known_lock:
        _known.clear()


def _tooled(specs, mode):
    return [s for s in specs if getattr(s, "tools", None) and getattr(s, "run_tool", None) and _app_shows(s, mode)]


def warm(modes=WARM_MODES, timeout=WARM_TIMEOUT):
    """List every assistant app's tools in ``modes`` so a conversation never
    starts on a cold cache (after a restart, say). Returns ``{mode: [ids missing]}``.
    """
    missing = {}
    specs = usable_specs()
    for mode in modes:
        jobs = [_tools_job(s, mode) for s in _tooled(specs, mode)]
        results = status._gather(jobs, timeout, executors=_EXECUTORS, fallback=_known_tools) if jobs else {}
        _remember(results, jobs)
        missing[mode] = _missing(results, jobs)
    return missing


def _missing(results, jobs):
    return [plugin_id for kind, plugin_id, *_ in jobs
            if (results.get((kind, plugin_id)) or {"missing": True}).get("missing")]


def warm_soon():
    """:func:`warm` in the background, in the server process only (e.g. once the
    admin lets the assistant use more apps)."""
    from .events import is_server

    if not is_server():
        return None

    def run():
        try:
            warm()
        except Exception:
            log.exception("warming assistant tools failed")
        finally:
            close_old_connections()

    thread = threading.Thread(target=run, name="hub-tools-warm", daemon=True)
    thread.start()
    return thread


def _app_shows(spec, mode):
    return status._visible(spec, mode) if spec.tiles else True


def _entry(name, title, description, target, params=None, readonly=False, confirm=False):
    return {"name": name, "title": title, "description": description,
            "parameters": params or {"type": "object", "properties": {}, "required": [],
                                     "additionalProperties": False},
            "readonly": readonly, "confirm": confirm, "target": target}


def _add(catalog, entry):
    name = entry["name"]
    if name in catalog:
        digest = hashlib.sha1(json.dumps(entry["target"]).encode()).hexdigest()[:8]
        name = entry["name"] = f"{name[:55]}_{digest}"
        if name in catalog:
            log.warning("dropping duplicate assistant tool %s", name)
            return
    catalog[name] = entry


def catalog(mode, timeout=LIST_TIMEOUT):
    """``{name: entry}`` for every tool offered in ``mode`` right now.

    An entry is what ``tools.list`` shows plus ``target``, what it runs.
    """
    specs = usable_specs()
    labels = {s.id: quick.app_label(s) for s in specs}
    bar = quick.prefs()
    tooled = _tooled(specs, mode)
    shortcutted = [s for s in specs if s.shortcuts and s.run_shortcut and quick.app_allows(bar, s.id, mode)]
    actioned = [s for s in specs if s.status and s.run_action and status._visible(s, mode)]
    tools_jobs = [_tools_job(s, mode) for s in tooled]
    started = time.monotonic()
    results = status._gather(
        tools_jobs
        + [("shortcuts", s.id, s.shortcuts, status._shortcuts_value, status.SHORTCUTS_TTL)
           for s in shortcutted]
        + [status._status_job(s, mode) for s in actioned],
        timeout, grace=status.REFRESH_GRACE, executors=_EXECUTORS, fallback=_known_tools)
    _remember(results, tools_jobs)
    missing = _missing(results, tools_jobs)
    if missing:
        log.warning("assistant tools listing (%s) took %.1fs without %s (never listed successfully yet)",
                    mode, time.monotonic() - started, ", ".join(missing))

    def value(kind, plugin_id):
        entry = results.get((kind, plugin_id)) or {}
        return entry.get("value") or ()

    out = {}
    _add(out, _entry(STATUS_TOOL, "Home status",
                     "What the hub's home screen shows right now: each app's status, readings "
                     "(temperatures etc.) and the lodge card. Use it to answer questions about the lodge.",
                     ["status"], readonly=True))
    for spec in tooled:
        for tool in value("tools", spec.id):
            if mode in tool["modes"]:
                _add(out, _entry(tool_name(spec.id, tool["id"]), tool["title"], tool["description"],
                                 ["tool", spec.id, tool["id"]], tool["params"], tool["readonly"],
                                 tool["confirm"]))
    for spec in shortcutted:
        for sc in status._offered(value("shortcuts", spec.id), mode):
            _add(out, _entry(tool_name(spec.id, "shortcut", sc["id"]), sc["title"][:60],
                             f"{labels[spec.id]}: run the \"{sc['title']}\" shortcut."[:300],
                             ["shortcut", spec.id, sc["id"]]))
    for spec in actioned:
        current = value("status", spec.id) or {}
        buttons = [("", None, a) for a in current.get("actions", ())]
        buttons += [(r["id"], r, a) for r in current.get("rows", ()) for a in r.get("actions", ())]
        for row_id, row, action in buttons:
            where = f"{labels[spec.id]}, {row['title']}" if row else labels[spec.id]
            _add(out, _entry(tool_name(spec.id, "action", row_id, action["id"]), action["label"][:60] or action["id"],
                             f"{where}: press the \"{action['label']}\" button."[:300],
                             ["action", spec.id, row_id, action["id"]]))
    if mode == DASHBOARD and not dashboard_control():
        out = {name: e for name, e in out.items() if e["readonly"]}
    return out


def listing(mode, timeout=LIST_TIMEOUT):
    """The tools offered in ``mode``, as ``tools.list`` returns them."""
    return [{k: v for k, v in e.items() if k != "target"} for e in catalog(mode, timeout).values()]


# --- calling -----------------------------------------------------------------

def home_summary(mode):
    """A compact, model-friendly view of the homepage in ``mode``."""
    data = status.home_data(mode)
    labels = {s.id: quick.app_label(s) for s in usable_specs()}
    apps = []
    for plugin_id, st in sorted(data["status"].items()):
        if plugin_id not in labels:
            continue
        item = {"app": labels.get(plugin_id, plugin_id), "status": st.get("text") or ""}
        if st.get("readings"):
            item["readings"] = [f"{r['label']}: {r['value']}{r['unit'] or ''}" for r in st["readings"]]
        if st.get("rows"):
            item["items"] = [f"{r['title']}: {r['detail']}" if r.get("detail") else r["title"]
                             for r in st["rows"]]
        if st.get("stale"):
            item["stale"] = True
        apps.append(item)
    out = {"mode": mode, "apps": apps}
    # The lodge card is the hub's own header (place, weather): kept unless its app opted out.
    card_spec = next((s for s in plugins() if s.card), None)
    card = status.lodge_card(mode) if card_spec is not None and opted_in(card_spec) else None
    if card:
        out["lodge"] = {k: card[k] for k in ("title", "detail") if card.get(k)}
        out["lodge"]["readings"] = [f"{r['label']}: {r['value']}{r['unit'] or ''}" for r in card["readings"]]
        if card.get("alerts"):
            out["lodge"]["alerts"] = card["alerts"]
    return out


def _run_app_tool(plugin_id, tool_id, args, mode):
    spec = next((s for s in plugins() if s.id == plugin_id and getattr(s, "run_tool", None)), None)
    if spec is None:
        raise ToolError("that tool isn't available any more")
    hook = status._resolve(spec.run_tool)
    if status._takes_mode(spec.run_tool):
        result = hook(tool_id, args, mode=mode)
    else:
        result = hook(tool_id, args)
    result = tool_result(result)
    return "Done." if result is None else result


def _execute(target, args, mode):
    try:
        kind = target[0]
        if kind == "status":
            return home_summary(mode)
        if kind == "tool":
            return _run_app_tool(target[1], target[2], args, mode)
        if kind == "shortcut":
            return status.run_shortcut(target[1], target[2], mode) or "Done."
        if kind == "action":
            return status.run_action(target[1], target[2], target[3], mode) or "Done."
        raise ValueError(f"unknown target {target!r}")
    finally:
        close_old_connections()


def call(session, name, args, timeout=CALL_TIMEOUT):
    """Run tool ``name`` for ``session``: ``{"ok": True, "result"}`` or ``{"ok": False, "error"}``.

    The tool must be offered in the session's mode as of now, and ``args``
    must fit its schema, before anything is run.
    """
    mode = session_mode(session["kind"])
    entry = catalog(mode).get(name) if isinstance(name, str) else None
    clean = None
    if entry is None:
        answer = {"ok": False, "error": f"no tool {name!r} is available"}
    else:
        try:
            clean = validate_args(entry["parameters"], args)
        except InvalidArgs as exc:
            answer = {"ok": False, "error": f"invalid arguments: {exc}"}
        else:
            answer = _run(entry, clean, mode, timeout)
    _publish(session["sid"], "call", {
        "sid": session["sid"], "mode": mode, "kind": session["kind"], "display": session.get("display"),
        "app": session["app"], "tool": name if isinstance(name, str) else None,
        "args": clean if clean is not None else (args if isinstance(args, dict) else None),
        "ok": answer["ok"], "error": answer.get("error"),
        "result": _short(answer.get("result")) if answer["ok"] else None, "time": time.time()})
    return answer


def _run(entry, args, mode, timeout):
    target = entry["target"]
    if target[0] != "status" and target[1] not in {s.id for s in usable_specs()}:
        return {"ok": False, "error": "that isn't available any more"}
    future = _executor.submit(_execute, entry["target"], args, mode)
    try:
        result = future.result(timeout=timeout)
    except FutureTimeout:
        log.warning("assistant tool %s timed out", entry["name"])
        return {"ok": False, "error": "the tool took too long"}
    except (status.ShortcutNotFound, status.ActionNotFound):
        return {"ok": False, "error": "that isn't available any more"}
    except ToolError as exc:
        return {"ok": False, "error": str(exc)[:300] or "the tool failed"}
    except Exception:
        log.exception("assistant tool %s failed", entry["name"])
        return {"ok": False, "error": "the tool failed"}
    try:
        size = len(json.dumps(result))
    except (TypeError, ValueError):
        log.warning("assistant tool %s returned something that isn't JSON", entry["name"])
        return {"ok": False, "error": "the tool failed"}
    if size > MAX_RESULT:
        log.warning("assistant tool %s returned %d bytes", entry["name"], size)
        return {"ok": False, "error": "the tool's answer was too long"}
    return {"ok": True, "result": result}


def _short(result):
    text = result if isinstance(result, str) else json.dumps(result)
    return text if len(text) <= AUDIT_RESULT else text[:AUDIT_RESULT - 1] + "…"


def _publish(sid, what, data):
    try:
        bus.publish(f"assistant/{sid}/{what}", data)
    except Exception:
        log.exception("couldn't publish assistant/%s/%s", sid, what)
