"""Settings -> Apps: what's installed, the store, installing, and managing an app.

Container apps come from the catalog and run under the supervisor; in-process
apps (built in to this hub's image) are listed but can't be managed here.
Installs and updates run as background jobs (:mod:`broker.jobs`); the pages
refresh themselves while one runs.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from broker import catalog, jobs, services, shares, supervisor
from broker.models import AppInstall, AuditEntry, ShareBlob, ShareGrant
from hub.modes import plugins

from .views import _icon, _label, _render, _settings_url, _version, saved_redirect, staff_page

log = logging.getLogger(__name__)

LOG_LINES = 200
MB = 1024 * 1024
UPDATED_FOR = timedelta(days=7)

LIFECYCLES = {"always": "always on", "on_demand": "starts when opened"}

# (prefix, icon, title, detail-or-None); detail None shows what follows the prefix.
PERMISSIONS = (
    ("ha.read:", "fa-house-signal", "Read Home Assistant", None),
    ("ha.call:", "fa-sliders", "Control Home Assistant", None),
    ("ha.ws", "fa-bolt", "Live Home Assistant updates", ""),
    ("speakers", "fa-volume-high", "Announce on speakers", ""),
    ("alerts", "fa-bell", "Show alerts on the hub", ""),
    ("media", "fa-photo-film", "Upload media", ""),
    ("events.subscribe:", "fa-tower-broadcast", "Listen to hub events", None),
    ("events.publish:", "fa-paper-plane", "Send hub events", None),
)

NETWORK_INFO = {
    "none": None,
    "outbound": ("fa-globe", "Internet access", "Can reach the internet, not your local network"),
    "lan": ("fa-network-wired", "Local network", "Can reach devices on your local network"),
}


def budget_mb():
    return int(getattr(settings, "BDH_APP_MEMORY_BUDGET_MB", 1400) or 1400)


def describe(permission):
    """``{icon, title, detail}`` for a permission string."""
    for prefix, icon, title, detail in PERMISSIONS:
        if permission == prefix or (prefix.endswith(":") and permission.startswith(prefix)):
            if detail is None:
                rest = permission[len(prefix):]
                detail = "everything" if rest in ("*", "#") else rest
            return {"icon": icon, "title": title, "detail": detail}
    return {"icon": "fa-key", "title": permission, "detail": ""}


def _short(permissions):
    """One line summarising granted permissions."""
    seen = []
    for p in permissions:
        d = describe(p)
        text = d["title"] + (f" ({d['detail']})" if d["detail"] else "")
        if text not in seen:
            seen.append(text)
    return " · ".join(seen) or "None"


def _catalog():
    """The cached catalog; a stale one is refreshed in the background."""
    try:
        return catalog.get_cached()[0]
    except catalog.CatalogError as exc:
        log.warning("catalog: %s", exc)
        return None


def _meta(app_id, data):
    entry = catalog.app(app_id, data) or {}
    return {"name": entry.get("name") or app_id, "description": entry.get("description") or "",
            "icon": entry.get("icon") or ""}


def _spec(app_id):
    return next((s for s in plugins() if s.id == app_id), None)


def _live(app_id):
    """Supervisor view + memory for one app; {} when the supervisor can't say."""
    out = {}
    try:
        out = supervisor.get_app(app_id) or {}
        if out.get("running"):
            out["memory_bytes"] = (supervisor.stats(app_id) or {}).get("memory_bytes")
    except supervisor.SupervisorError as exc:
        out = {"unreachable": str(exc)}
    return out


def _live_all(app_ids):
    if not app_ids or not supervisor.enabled():
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(app_ids))) as pool:
        return dict(zip(app_ids, pool.map(_live, app_ids)))


def _status(record, live, job):
    """``(colour, text)`` for the status dot."""
    if job and job["state"] == jobs.RUNNING:
        return "y", job["label"] + "…"
    if job and job["state"] == jobs.FAILED:
        return "r", f"{job['label']} failed"
    if "unreachable" in live:
        return "", "Unknown"
    state = live.get("status")
    if state == "failed":
        return "r", "Install failed"
    if state and state != "installed":
        return "y", state.replace("_", " ").capitalize()
    if not record.enabled:
        return "y", "Finishing install"
    if live.get("running"):
        return "g", "Running"
    if live.get("failures"):
        return "r", "Crashed · retrying"
    if (live.get("lifecycle") or record.lifecycle) == "on_demand":
        return "y", "Sleeping"
    return "", "Stopped"


def _live_fields(record, live, job):
    """Status and memory for a row, from the supervisor's ``live`` view."""
    limit = live.get("memory_mb") or record.memory_mb
    used = live.get("memory_bytes")
    used_mb = round(used / MB) if used else None
    dot, text = _status(record, live, job)
    return {"dot": dot, "status": text, "used_mb": used_mb, "limit_mb": limit,
            "pct": min(100, round(100 * used_mb / limit)) if used_mb and limit else 0}


def _row(record, data):
    """A row without the supervisor's view; the page fetches that afterwards."""
    meta = _meta(record.app_id, data)
    spec = _spec(record.app_id)
    job = jobs.get(record.app_id)
    if job and job["state"] in (jobs.RUNNING, jobs.FAILED):
        dot, text = _status(record, {}, job)
    else:
        dot, text = "", ""
    update = services.update_available(record, data) if data else None
    extra = services.widened(record, update) if update else {}
    return {
        "id": record.app_id, "label": _label(spec) if spec else meta["name"],
        "img": (_icon(spec) if spec else "") or meta["icon"], "version": record.version,
        "lifecycle": LIFECYCLES.get(record.lifecycle, ""), "lan": record.approved_network == "lan",
        "dot": dot, "status": text, "busy": bool(job and job["state"] == jobs.RUNNING),
        "beta": catalog.is_beta(_catalog_entry(record.app_id, record.version, data)),
        "update": update["version"] if update else "",
        "update_beta": catalog.is_beta(update),
        "approval": bool(extra), "asks": _asks(extra),
        "updated": _recently_updated(record),
        "manage_url": reverse("hub_settings_app_manage", args=[record.app_id]),
    }


def _asks(extra):
    """One line naming what an update would newly get, e.g. "Show alerts on the hub · Local network"."""
    parts = [describe(p)["title"] for p in extra.get("permissions", ())]
    if extra.get("network") in NETWORK_INFO and NETWORK_INFO[extra["network"]]:
        parts.append(NETWORK_INFO[extra["network"]][1])
    parts += [f"Helper service: {c}" for c in extra.get("companions", ())]
    return " · ".join(dict.fromkeys(parts))


def _recently_updated(record):
    changed = record.version_changed
    return bool(changed and timezone.now() - changed < UPDATED_FOR)


def _catalog_entry(app_id, version, data):
    """The catalog's entry for ``app_id`` at ``version`` (usable here or not), or None."""
    if data is None:
        return None
    app = catalog.app(app_id, data) or {}
    for entry in app.get("versions") or ():
        if isinstance(entry, dict) and entry.get("version") == version:
            return entry
    return None


def _changelog(record, data):
    """Release notes for the installed version, from the catalog."""
    entry = _catalog_entry(record.app_id, record.version, data)
    return _notes(entry)


def _notes(entry):
    return str(entry.get("changelog") or "").strip() if entry else ""


def _container_records():
    return list(AppInstall.objects.filter(runtime=AppInstall.CONTAINER).order_by("app_id"))


def _builtin_rows(skip):
    rows = []
    for spec in getattr(settings, "HUB_PLUGINS", ()):
        if spec.id in skip:
            continue
        rows.append({"id": spec.id, "label": _label(spec), "img": _icon(spec), "version": _version(spec),
                     "builtin": True, "settings_url": _settings_url(spec)})
    return rows


def _tabs(active):
    return [{"label": "Installed", "url": reverse("hub_settings_apps"), "on": active == "installed"},
            {"label": "Store", "url": reverse("hub_settings_store"), "on": active == "store"}]


@staff_page
def installed(request):
    data = _catalog()
    records = _container_records()
    rows = [_row(r, data) for r in records]
    builtin = _builtin_rows({r.app_id for r in records})
    approval = [r for r in rows if r["update"] and r["approval"]]
    return _render(request, "hub_settings/apps.html", "apps",
                   tabs=_tabs("installed"), rows=[r for r in rows if r not in approval],
                   approval=approval, builtin=builtin,
                   total=len(rows) + len(builtin), budget_mb=budget_mb(),
                   updates=sum(1 for r in rows if r["update"] and not r["approval"]),
                   busy=any(r["busy"] for r in rows) or jobs.running("*"),
                   enabled=supervisor.enabled(), catalog_ok=data is not None)


@staff_page
def installed_live(request):
    """Status and memory for every installed app (the Apps page fills these in)."""
    records = _container_records()
    live = _live_all([r.app_id for r in records])
    apps = {r.app_id: _live_fields(r, live.get(r.app_id, {}), jobs.get(r.app_id)) for r in records}
    return JsonResponse({"apps": apps, "used_mb": sum(a["used_mb"] or 0 for a in apps.values()),
                         "budget_mb": budget_mb()})


@staff_page
def store(request):
    data = _catalog()
    have = {r.app_id for r in AppInstall.objects.all()} | {s.id for s in plugins()}
    apps = []
    for app_id, entry in sorted(catalog.apps(data).items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower()):
        if app_id in have:
            continue
        usable = [v for v in catalog.versions(app_id, data) if v.get("hub_api") == catalog.hub_api()]
        if not usable:
            continue
        newest = usable[0]
        apps.append({"id": app_id, "name": entry.get("name") or app_id, "description": entry.get("description") or "",
                     "icon": entry.get("icon") or "", "version": newest["version"],
                     "beta": catalog.is_beta(newest),
                     "lan": newest.get("network") == "lan",
                     "url": reverse("hub_settings_store_app", args=[app_id])})
    return _render(request, "hub_settings/store.html", "apps", tabs=_tabs("store"), apps=apps,
                   enabled=supervisor.enabled(), catalog_ok=data is not None)


DEFAULT_MEMORY_MB = 96  # the supervisor's limit when a manifest doesn't set one


def _fits(entry):
    """Whether ``entry``'s memory limit fits in what's left of the app budget."""
    used = sum(r.memory_mb or DEFAULT_MEMORY_MB for r in _container_records())
    return (entry.get("memory_mb") or DEFAULT_MEMORY_MB) <= budget_mb() - used


NO_ROOM = "There isn't enough memory for this app. Uninstall another app first."


@staff_page
def store_app(request, app_id):
    data = _catalog()
    if catalog.app(app_id, data) is None:
        raise Http404("no such app")
    if AppInstall.objects.filter(app_id=app_id).exists():
        return redirect("hub_settings_app_manage", app_id)
    try:
        entry = services._entry(app_id, None)
    except (catalog.CatalogError, services.ServiceError) as exc:
        messages.error(request, str(exc))
        return redirect("hub_settings_store")
    want = services.requested(entry)
    if request.method == "POST":
        if not supervisor.enabled():
            messages.error(request, "App containers aren't set up on this hub.")
            return redirect("hub_settings_store")
        if not _fits(entry):
            messages.error(request, NO_ROOM)
            return redirect("hub_settings_store_app", app_id)
        chosen = [p for p in request.POST.getlist("perm") if p in want["permissions"]]
        try:
            services.install(app_id, entry["version"], permissions=chosen, wait=False)
        except (catalog.CatalogError, services.ServiceError, supervisor.SupervisorError) as exc:
            messages.error(request, f"Couldn't install: {exc}")
            return redirect("hub_settings_store_app", app_id)
        jobs.start(app_id, "Installing", services.finish, app_id)
        return redirect("hub_settings_app_manage", app_id)
    meta = _meta(app_id, data)
    return _render(request, "hub_settings/store_app.html", "apps", app=meta, app_id=app_id,
                   version=entry["version"], beta=catalog.is_beta(entry),
                   changelog=entry.get("changelog") or "",
                   perms=[{"value": p, **describe(p)} for p in want["permissions"]],
                   network=NETWORK_INFO.get(want["network"]), lan=want["network"] == "lan",
                   companions=want["companions"], fits=_fits(entry), no_room=NO_ROOM,
                   enabled=supervisor.enabled())


def _record(app_id):
    record = AppInstall.objects.filter(app_id=app_id, runtime=AppInstall.CONTAINER).first()
    if record is None:
        raise Http404("no such app")
    return record


def _heal(record, live):
    """Finish an install the hub lost track of (it restarted mid-install)."""
    if (not record.enabled and live.get("status") == "installed" and not jobs.running(record.app_id)
            and not jobs.get(record.app_id)):
        jobs.start(record.app_id, "Finishing install", services.finish, record.app_id)


@staff_page
def manage(request, app_id):
    record = _record(app_id)
    live = _live(app_id) if supervisor.enabled() else {}
    _heal(record, live)
    data = _catalog()
    row = {**_row(record, data), **_live_fields(record, live, jobs.get(app_id))}
    logs = ""
    if supervisor.enabled() and "unreachable" not in live:
        try:
            logs = supervisor.logs(app_id, lines=LOG_LINES)
        except supervisor.SupervisorError as exc:
            logs = f"(logs unavailable: {exc})"
    spec = _spec(app_id)
    job = jobs.get(app_id)
    granted = set(record.granted_permissions or ())
    settings_url = _settings_url(spec) if spec else ""
    return _render(request, "hub_settings/app_manage.html", "apps", app=row, record=record,
                   job=job, error=(live.get("error") if live.get("status") == "failed" else ""),
                   logs=logs, settings_url=settings_url,
                   perms=[{"value": p, "on": p in granted, **describe(p)}
                          for p in record.requested_permissions or ()],
                   summary=_short([p for p in record.requested_permissions or () if p in granted]),
                   network=NETWORK_INFO.get(record.approved_network),
                   changelog=_changelog(record, data),
                   sharing=_sharing(record), receives=_receives(record),
                   review=_review(record, data), done=_done(record, job, settings_url))


def _review(record, data):
    """The review card for an update that needs approval, or None."""
    entry = services.update_available(record, data) if data is not None else None
    if entry is None:
        return None
    extra = services.widened(record, entry)
    if not extra:
        return None
    want = services.requested(entry)
    items = [{**describe(p), "new": True} for p in extra.get("permissions", ())]
    net = NETWORK_INFO.get(want["network"])
    if "network" in extra and net:
        items.append({"icon": net[0], "title": net[1], "detail": net[2], "new": True})
    elif services._rank(want["network"]) < services._rank(record.approved_network):
        old = NETWORK_INFO.get(record.approved_network)
        items.append({"icon": net[0] if net else "fa-ban", "title": net[1] + " only" if net else "No network access",
                      "detail": f"Less access than now: it no longer needs {old[1].lower()}" if old else "",
                      "less": True})
    items += [{"icon": "fa-cubes", "title": f"Helper service: {c}", "detail": "", "new": True}
              for c in extra.get("companions", ())]
    return {"version": entry["version"], "beta": catalog.is_beta(entry), "changelog": _notes(entry),
            "items": items}


def _done(record, job, settings_url):
    """The "Updated" card after an update job finished, or None."""
    info = (job or {}).get("info") or {}
    if not job or job["state"] != jobs.DONE or info.get("version") != record.version:
        return None
    added = info.get("added") or []
    return {"version": record.version, "added": added,
            "settings_url": settings_url if info.get("approved") else ""}


def _back(app_id):
    return redirect("hub_settings_app_manage", app_id)


def _app_name(record):
    return catalog.display_name(record)


def _mb(size):
    return f"{size / (1024 * 1024):.1f} MB" if size >= 100 * 1024 else f"{max(1, round(size / 1024))} KB"


def _sharing(record):
    """The Sharing card: each share ``record`` declares, with a checkbox per app."""
    declared = shares.declared(record.manifest)
    if not declared:
        return []
    others = sorted((r for r in AppInstall.objects.filter(runtime=AppInstall.CONTAINER)
                     .exclude(app_id=record.app_id)), key=lambda r: _app_name(r).lower())
    grants = set(ShareGrant.objects.filter(provider=record.app_id).values_list("name", "consumer"))
    out = []
    for share in declared:
        rev = shares.latest(record.app_id, share["name"])
        size = 0
        if rev:
            size = sum(ShareBlob.objects.filter(owner=record.app_id, sha256__in={
                rev.main_sha.sha256, *(rev.attachments or {}).values()}).values_list("size", flat=True))
        apps = [{"id": r.app_id, "name": _app_name(r), "on": (share["name"], r.app_id) in grants,
                 "reads": share["type"] in shares.reads(r.manifest)} for r in others]
        out.append({**share, "revision": rev.revision if rev else 0, "updated": rev.created if rev else None,
                    "size": _mb(size) if rev else "", "attachments": len(rev.attachments or {}) if rev else 0,
                    "readers": [a for a in apps if a["reads"] or a["on"]],
                    "others": [a for a in apps if not a["reads"] and not a["on"]]})
    return out


def _receives(record):
    """``record``'s granted shares, for the read-only "Receives" line."""
    out = []
    installs = {r.app_id: r for r in AppInstall.objects.filter(
        app_id__in=ShareGrant.objects.filter(consumer=record.app_id).values("provider"))}
    for provider, name in ShareGrant.objects.filter(consumer=record.app_id).order_by("provider", "name") \
            .values_list("provider", "name"):
        install = installs.get(provider)
        share = shares.declaration(install, name)
        if share:
            out.append(f"{share['title']} (from {_app_name(install)})")
    return out


@staff_page
@require_POST
def manage_share_grant(request, app_id):
    """Check or uncheck an app on the Sharing card: grant or revoke one share."""
    record = _record(app_id)
    name, consumer = request.POST.get("share", ""), request.POST.get("consumer", "")
    if shares.declaration(record, name) is None:
        raise Http404("no such share")
    if consumer == app_id or not AppInstall.objects.filter(app_id=consumer, runtime=AppInstall.CONTAINER).exists():
        raise Http404("no such app")
    detail = json.dumps({"share": name, "consumer": consumer, "by": request.user.get_username()})
    if request.POST.get("on") == "on":
        ShareGrant.objects.get_or_create(consumer=consumer, provider=app_id, name=name,
                                         defaults={"granted_by": request.user.get_username()})
        AuditEntry.record(app_id, "shares.grant", True, detail)
    else:
        ShareGrant.objects.filter(consumer=consumer, provider=app_id, name=name).delete()
        AuditEntry.record(app_id, "shares.revoke", True, detail)
    return redirect(reverse("hub_settings_app_manage", args=[app_id]) + "#sharing")


@staff_page
@require_POST
def manage_restart(request, app_id):
    _record(app_id)
    try:
        supervisor.restart(app_id)
        messages.success(request, "Restarted.")
    except supervisor.SupervisorError as exc:
        messages.error(request, f"Couldn't restart: {exc}")
    return _back(app_id)


def _update(app_id, version, approve):
    record = AppInstall.objects.get(app_id=app_id)
    services.upgrade(record, services._entry(app_id, version), approve=approve)


@staff_page
@require_POST
def manage_update(request, app_id):
    record = _record(app_id)
    to_list = request.POST.get("next") == "apps"
    back = redirect("hub_settings_apps") if to_list else _back(app_id)
    entry = services.update_available(record)
    if entry is None:
        messages.info(request, "Already up to date.")
        return back
    extra = services.widened(record, entry)
    if extra and request.POST.get("approve") != "1":
        return _back(app_id)  # the app page opens with the review card
    info = {"version": entry["version"], "approved": bool(extra),
            "added": [describe(p)["title"] for p in extra.get("permissions", ())]}
    if not jobs.start(app_id, "Updating", _update, app_id, entry["version"], bool(extra), info=info):
        messages.info(request, "Something is already running for this app.")
    elif to_list:
        messages.success(request, f"Updating {_app_name(record)} to v{entry['version']}.")
    return back


@staff_page
@require_POST
def manage_auto_update(request, app_id):
    record = _record(app_id)
    record.auto_update = request.POST.get("auto_update") == "on"
    record.save(update_fields=["auto_update", "updated"])
    return _back(app_id)


@staff_page
@require_POST
def manage_permissions(request, app_id):
    record = _record(app_id)
    services.set_permissions(app_id, [p for p in request.POST.getlist("perm")
                                      if p in (record.requested_permissions or ())])
    messages.success(request, "Permissions saved.")
    return saved_redirect(request, reverse("hub_settings_app_manage", args=[app_id]))


@staff_page
@require_POST
def manage_uninstall(request, app_id):
    _record(app_id)
    if jobs.running(app_id):
        messages.error(request, "Wait for the current job to finish first.")
        return _back(app_id)
    try:
        services.uninstall(app_id, purge=request.POST.get("purge") == "on")
    except (services.ServiceError, supervisor.SupervisorError) as exc:
        messages.error(request, f"Couldn't uninstall: {exc}")
        return _back(app_id)
    jobs.clear(app_id)
    messages.success(request, "Uninstalled.")
    return redirect("hub_settings_apps")


def _update_all():
    services.update_all()


@staff_page
@require_POST
def update_all(request):
    pending = services.pending_updates()
    ready = [r for r, e in pending if not services.widened(r, e)]
    held = [_app_name(r) for r, e in pending if services.widened(r, e)]
    wait = f" {', '.join(held)} {'needs' if len(held) == 1 else 'need'} your approval first." if held else ""
    if not ready:
        messages.info(request, "Nothing to update without your approval." + wait)
    elif not jobs.start("*", "Updating all", _update_all):
        messages.info(request, "An update is already running.")
    else:
        n = len(ready)
        messages.success(request, f"Updating {n} app{'s' if n != 1 else ''} in the background.{wait}")
    return redirect("hub_settings_apps")
