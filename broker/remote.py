"""Apps in their own containers, seen by the hub as plugin specs.

Each enabled :class:`~broker.models.AppInstall` becomes a :class:`RemoteSpec`
built from the manifest its runtime serves at ``GET /_bdh/manifest``. Its
hook attributes are markers (``remote:<app>:<hook>``) that
``hub.status._resolve`` turns into signed ``POST /_bdh/hook`` calls; the app
returns hook results already converted to the hub's JSON shapes, wrapped in
:class:`Converted` so the hub's converters pass them through.

App pages are on the app's own origin (``http://<hub host>:<app port>``) and
shown in an iframe by the shell page at ``/apps/<app>/<path>``. Paths an app
returns (images, card links, alert popups) are rewritten onto the hub:
``/apps/<app>/_raw/...`` for media, ``/apps/<app>/...`` for pages.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass, field

import requests
from bdh_sdk.identity import signed_headers
from bdh_sdk.plugins import ALL_MODES, CHOICE, MULTI, Setting
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import conf

log = logging.getLogger(__name__)

PREFIX = "remote:"
MANIFEST_TIMEOUT = 5.0
HOT_MANIFEST_TIMEOUT = 2.0
MANIFEST_RETRY = 60.0
HOOK_TIMEOUT = (2.0, 5.5)
REMOTE_HOOKS = ("status", "shortcuts", "run_shortcut", "run_action", "stay", "card")


class RemoteError(Exception):
    pass


class Converted:
    """A hook result the app already converted; the hub's converters return
    ``value`` untouched."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"Converted({self.value!r})"


# --- origins ---------------------------------------------------------------

def hostname(host):
    """``host`` (a Host header) without its port; IPv6 keeps its brackets."""
    host = (host or "").strip()
    if host.startswith("["):
        end = host.find("]")
        return host[:end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _origin(host, port):
    name = hostname(host) or "localhost"
    return f"http://{name}" if int(port) == 80 else f"http://{name}:{int(port)}"


def hub_origin(host):
    return _origin(host, conf.hub_port())


def app_origin(port, host):
    return _origin(host, port)


# --- calling an app --------------------------------------------------------

def _token(install):
    from .tokens import token_for

    return token_for(install)


def _upstream(install):
    if not install.upstream:
        raise RemoteError(f"{install.app_id} has no upstream")
    return install.upstream.rstrip("/")


def signed_request(install, method, path, payload=None, timeout=HOOK_TIMEOUT, identity=None, wake=True):
    """A signed call from the hub to the app's runtime; returns the Response."""
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = signed_headers(_token(install), method, path, identity=identity, body=body)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    url = _upstream(install) + path
    try:
        return requests.request(method, url, data=body or None, headers=headers, timeout=timeout)
    except requests.ConnectionError:
        # An on-demand container app may be stopped: start it and try once more.
        if not wake or not _wake(install):
            raise
    headers = signed_headers(_token(install), method, path, identity=identity, body=body)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return requests.request(method, url, data=body or None, headers=headers, timeout=timeout)


def _wake(install):
    from . import supervisor

    if not getattr(install, "is_container", False) or not supervisor.enabled():
        return False
    try:
        view = supervisor.start(install.app_id, wait=True)
    except supervisor.SupervisorError as exc:
        log.info("%s: couldn't start it: %s", install.app_id, exc)
        return False
    return view.get("healthy") is not False


def call_hook(app_id, hook, timeout=HOOK_TIMEOUT, **args):
    """Run ``hook`` in ``app_id``'s container and return its JSON result."""
    install = _install(app_id)
    payload = {"hook": hook, **{k: v for k, v in args.items() if v is not None}}
    try:
        resp = signed_request(install, "POST", "/_bdh/hook", payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RemoteError(f"{app_id} {hook}: {exc}") from exc
    if resp.status_code != 200:
        raise RemoteError(f"{app_id} {hook}: HTTP {resp.status_code}")
    try:
        return resp.json()["result"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RemoteError(f"{app_id} {hook}: bad reply") from exc


def fetch_manifest(install, timeout=MANIFEST_TIMEOUT, wake=True):
    """Fetch and save ``install``'s manifest; returns it (raises RemoteError)."""
    from .models import AppInstall

    try:
        resp = signed_request(install, "GET", "/_bdh/manifest", timeout=timeout, wake=wake)
    except requests.RequestException as exc:
        raise RemoteError(f"{install.app_id} manifest: {exc}") from exc
    if resp.status_code != 200:
        raise RemoteError(f"{install.app_id} manifest: HTTP {resp.status_code}")
    try:
        manifest = resp.json()
    except ValueError as exc:
        raise RemoteError(f"{install.app_id} manifest: not JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("id") != install.app_id:
        raise RemoteError(f"{install.app_id} manifest: wrong app id")
    now = timezone.now()
    fields = {
        "manifest": manifest,
        "manifest_fetched_at": now,
        "requested_permissions": sorted(p for p in manifest.get("permissions") or () if isinstance(p, str)),
    }
    for name in ("network", "lifecycle"):
        if isinstance(manifest.get(name), str):
            fields[name] = manifest[name]
    if isinstance(manifest.get("memory_mb"), int):
        fields["memory_mb"] = manifest["memory_mb"]
    # update() skips post_save, so the spec cache isn't thrown away mid-build.
    AppInstall.objects.filter(pk=install.pk).update(**fields)
    for name, value in fields.items():
        setattr(install, name, value)
    return manifest


# --- URL rewriting -----------------------------------------------------------

def _app_path(path):
    return isinstance(path, str) and path.startswith("/") and not path.startswith("//") and "\\" not in path


def page_url(app_id, path):
    """A page of the app, shown in the hub's shell."""
    return f"/apps/{app_id}{path}" if _app_path(path) else None


def raw_url(app_id, path):
    """Media from the app (an image, a sound), fetched through the hub."""
    if isinstance(path, str) and path.startswith(("http://", "https://")):
        return path
    return f"/apps/{app_id}/_raw{path}" if _app_path(path) else ""


def static_url(app_id, icon):
    if isinstance(icon, str) and icon.startswith(("http://", "https://", "/")):
        return raw_url(app_id, icon)
    return raw_url(app_id, "/static/" + str(icon or "").lstrip("/"))


def _fix_status(app_id, value):
    if isinstance(value, dict):
        for row in value.get("rows") or ():
            if isinstance(row, dict):
                row["image"] = raw_url(app_id, row.get("image"))
    return value


def _fix_card(app_id, value):
    if isinstance(value, dict) and value.get("url"):
        value["url"] = page_url(app_id, value["url"])
    return value


def _fix_stay(_app_id, value):
    if isinstance(value, dict):
        for key in ("arrival", "checkout"):
            if isinstance(value.get(key), str):
                value[key] = parse_datetime(value[key])
    return value


def _fix_backgrounds(app_id, value):
    for bg in value or ():
        if isinstance(bg, dict):
            bg["url"] = raw_url(app_id, bg.get("url"))
    return [bg for bg in value or () if isinstance(bg, dict) and bg.get("url")]


_FIXES = {"status": _fix_status, "card": _fix_card, "stay": _fix_stay,
          "provide:backgrounds": _fix_backgrounds}


def parse_marker(path):
    """``(app_id, hook)`` from a ``remote:`` marker, else None."""
    if not isinstance(path, str) or not path.startswith(PREFIX):
        return None
    app_id, _, hook = path[len(PREFIX):].partition(":")
    return app_id, hook


def is_marker(path):
    return parse_marker(path) is not None


def hook(path):
    """The callable ``hub.status`` runs for the marker ``path``."""
    app_id, name = parse_marker(path)
    fix = _FIXES.get(name, lambda _id, v: v)

    if name == "run_shortcut":
        def run(shortcut_id, mode=None):
            return call_hook(app_id, "run_shortcut", shortcut_id=shortcut_id, mode=mode)
    elif name == "run_action":
        def run(row_id, action_id, mode=None):
            return call_hook(app_id, "run_action", row_id=row_id, action_id=action_id, mode=mode)
    elif name.startswith("provide:"):
        capability = name.split(":", 1)[1]

        def run(mode=None):
            return Converted(fix(app_id, call_hook(app_id, "provide", capability=capability)))
    else:
        def run(mode=None):
            return Converted(fix(app_id, call_hook(app_id, name, mode=mode)))
    run.__qualname__ = f"remote[{app_id}].{name}"
    return run


# --- specs -------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteTile:
    title: str
    url_name: str
    url: str
    icon: str
    section: str
    subtitle: str = ""
    order: int = 100
    modes: frozenset = ALL_MODES


@dataclass(frozen=True)
class RemotePage:
    title: str
    url: str
    subtitle: str = ""
    icon: str = "fas fa-sliders"
    url_name: str = ""


@dataclass(frozen=True)
class RemoteSetting(Setting):
    """A setting declared by a container app. Dynamic choices come from its
    ``setting_options`` hook."""

    app_id: str = ""

    @property
    def options(self):
        if self.choices_from:
            try:
                pairs = call_hook(self.app_id, "setting_options", key=self.key)
                return tuple((p[0], str(p[1])) for p in pairs or ())
            except Exception:
                log.warning("choices for %s setting %r failed", self.app_id, self.key, exc_info=True)
                return ()
        return Setting.options.fget(self)


@dataclass(frozen=True)
class RemoteSpec:
    """Quacks like ``bdh_sdk.plugins.PluginSpec`` for the parts the hub reads."""

    id: str
    port: int
    version: str = ""
    tiles: tuple = ()
    status: str | None = None
    shortcuts: str | None = None
    run_shortcut: str | None = None
    run_action: str | None = None
    stay: str | None = None
    card: str | None = None
    settings: tuple = ()
    settings_pages: tuple = ()
    provides: dict = field(default_factory=dict, hash=False)
    stat_keys: tuple = ()
    permissions: tuple = ()
    app: None = None
    urls: None = None
    websocket_routes: None = None
    remote: bool = True

    @property
    def prefix(self):
        return f"apps/{self.id}/"


def _tiles(app_id, manifest):
    from hub.plugins import SECTIONS

    tiles = []
    for t in manifest.get("tiles") or ():
        url = page_url(app_id, t.get("url"))
        if not url or t.get("section") not in SECTIONS or not t.get("title"):
            continue
        modes = frozenset(m for m in t.get("modes") or () if m in ALL_MODES) or ALL_MODES
        tiles.append(RemoteTile(
            title=str(t["title"]), url_name=str(t.get("url_name") or t["url"]), url=url,
            icon=static_url(app_id, t.get("icon")), section=t["section"],
            subtitle=str(t.get("subtitle") or ""), order=int(t.get("order") or 100), modes=modes))
    return tuple(tiles)


def _settings(app_id, manifest):
    out = []
    for s in manifest.get("settings") or ():
        try:
            dynamic = bool(s.get("dynamic_choices"))
            choices = tuple(tuple(c) if isinstance(c, list) else c for c in s.get("choices") or ())
            if s.get("type") in (CHOICE, MULTI) and not dynamic and not choices:
                continue
            out.append(RemoteSetting(
                key=s["key"], label=s["label"], type=s.get("type") or "str", default=s.get("default"),
                help=s.get("help") or "", choices=choices,
                choices_from=f"{PREFIX}{app_id}" if dynamic else "",
                minimum=s.get("minimum"), maximum=s.get("maximum"), unit=s.get("unit") or "",
                app_id=app_id))
        except (KeyError, TypeError, ValueError):
            log.warning("%s: skipping bad setting %r", app_id, s)
    return tuple(out)


def _pages(app_id, manifest):
    pages = []
    for p in manifest.get("settings_pages") or ():
        url = page_url(app_id, p.get("url"))
        if url and p.get("title"):
            pages.append(RemotePage(title=str(p["title"]), url=url, subtitle=str(p.get("subtitle") or ""),
                                    icon=str(p.get("icon") or "fas fa-sliders")))
    return tuple(pages)


def spec_from_manifest(install, manifest):
    app_id = install.app_id
    hooks = set(manifest.get("hooks") or ())
    markers = {h: f"{PREFIX}{app_id}:{h}" if h in hooks else None for h in REMOTE_HOOKS}
    provides = {cap: f"{PREFIX}{app_id}:provide:{cap}"
                for cap in manifest.get("provides") or () if isinstance(cap, str)}
    stat_keys = tuple((str(k), str(v)) for k, v in manifest.get("stat_keys") or ())
    return RemoteSpec(
        id=app_id, port=install.port, version=install.version or str(manifest.get("version") or ""),
        tiles=_tiles(app_id, manifest), settings=_settings(app_id, manifest),
        settings_pages=_pages(app_id, manifest), provides=provides, stat_keys=stat_keys,
        permissions=tuple(manifest.get("permissions") or ()), **markers)


_lock = threading.Lock()
_specs = None
_installs = {}
_last_attempt = {}


def installs_changed(instance=None, signal=None, **_kwargs):
    from django.db.models.signals import post_delete

    from . import events_bridge

    global _specs
    with _lock:
        _specs = None
        _installs.clear()
    if instance is not None and (signal is post_delete or not instance.enabled):
        events_bridge.drop(instance.app_id)


def _install(app_id):
    with _lock:
        install = _installs.get(app_id)
    if install is None:
        from .models import AppInstall

        install = AppInstall.objects.get(app_id=app_id, enabled=True)
    return install


def _load(local_ids):
    from .models import AppInstall

    specs, installs, missing = [], {}, False
    for install in AppInstall.objects.filter(enabled=True):
        if install.app_id in local_ids:
            log.warning("container app %s clashes with an in-process app; ignored", install.app_id)
            continue
        installs[install.app_id] = install
        manifest = install.manifest
        if manifest is None and install.upstream:
            now = time.monotonic()
            if now - _last_attempt.get(install.app_id, -MANIFEST_RETRY) >= MANIFEST_RETRY:
                _last_attempt[install.app_id] = now
                try:
                    manifest = fetch_manifest(install, timeout=HOT_MANIFEST_TIMEOUT, wake=False)
                except RemoteError as exc:
                    log.warning("%s", exc)
        if manifest is None:
            missing = True
            continue
        try:
            specs.append(spec_from_manifest(install, manifest))
        except Exception:
            log.exception("%s: bad manifest", install.app_id)
    return tuple(specs), installs, missing


def remote_specs(local_ids=()):
    """Specs for every enabled container app with a manifest (cached)."""
    global _specs
    with _lock:
        cached = _specs
    # Re-read every minute too: `broker_refresh` runs in another process.
    if cached is not None and time.monotonic() - cached[3] < MANIFEST_RETRY:
        return cached[0]
    try:
        specs, installs, missing = _load(set(local_ids))
    except Exception as exc:
        # Before migrations or from async code: keep whatever we had.
        log.debug("remote specs unavailable: %s", exc)
        return cached[0] if cached else ()
    with _lock:
        _specs = (specs, installs, missing, time.monotonic())
        _installs.clear()
        _installs.update(installs)
    return specs


def spec_for(app_id):
    from hub.modes import plugins

    return next((s for s in plugins() if s.id == app_id and getattr(s, "remote", False)), None)


def install_for_port(port):
    """The enabled install on public ``port`` (sync; DB)."""
    from .models import AppInstall

    return AppInstall.objects.filter(port=port, enabled=True).first()
