"""Settings -> Apps: what's installed, the store, installing, and managing an app.

Container apps come from the catalog and run under the supervisor; in-process
apps (built in to this hub's image) are listed but can't be managed here.
Installs and updates run as background jobs (:mod:`broker.jobs`); the pages
refresh themselves while one runs.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from broker import catalog, jobs, services, supervisor
from broker.models import AppInstall
from hub.modes import plugins

from .views import _icon, _label, _render, _settings_url, _version, staff_page

log = logging.getLogger(__name__)

LOG_LINES = 200
MB = 1024 * 1024

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
    try:
        return catalog.get()
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


def _row(record, live, data):
    meta = _meta(record.app_id, data)
    spec = _spec(record.app_id)
    job = jobs.get(record.app_id)
    limit = live.get("memory_mb") or record.memory_mb
    used = live.get("memory_bytes")
    used_mb = round(used / MB) if used else None
    dot, text = _status(record, live, job)
    update = services.update_available(record, data) if data else None
    return {
        "id": record.app_id, "label": _label(spec) if spec else meta["name"],
        "img": (_icon(spec) if spec else "") or meta["icon"], "version": record.version,
        "lifecycle": LIFECYCLES.get(record.lifecycle, ""), "lan": record.approved_network == "lan",
        "dot": dot, "status": text, "busy": bool(job and job["state"] == jobs.RUNNING),
        "used_mb": used_mb, "limit_mb": limit,
        "pct": min(100, round(100 * used_mb / limit)) if used_mb and limit else 0,
        "update": update["version"] if update else "",
        "manage_url": reverse("hub_settings_app_manage", args=[record.app_id]),
    }


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
    live = _live_all([r.app_id for r in records])
    rows = [_row(r, live.get(r.app_id, {}), data) for r in records]
    used = sum(r["used_mb"] or 0 for r in rows)
    builtin = _builtin_rows({r.app_id for r in records})
    return _render(request, "hub_settings/apps.html", "apps",
                   tabs=_tabs("installed"), rows=rows, builtin=builtin,
                   total=len(rows) + len(builtin), used_mb=used, budget_mb=budget_mb(),
                   budget_pct=min(100, round(100 * used / budget_mb())),
                   updates=sum(1 for r in rows if r["update"]),
                   busy=any(r["busy"] for r in rows) or jobs.running("*"),
                   enabled=supervisor.enabled(), catalog_ok=data is not None)


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
                     "memory_mb": newest.get("memory_mb"), "lifecycle": LIFECYCLES.get(newest.get("lifecycle"), ""),
                     "lan": newest.get("network") == "lan",
                     "url": reverse("hub_settings_store_app", args=[app_id])})
    return _render(request, "hub_settings/store.html", "apps", tabs=_tabs("store"), apps=apps,
                   enabled=supervisor.enabled(), catalog_ok=data is not None)


def _free_mb():
    records = _container_records()
    return budget_mb() - sum(r.memory_mb or 0 for r in records)


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
                   version=entry["version"], changelog=entry.get("changelog") or "",
                   perms=[{"value": p, **describe(p)} for p in want["permissions"]],
                   network=NETWORK_INFO.get(want["network"]), lan=want["network"] == "lan",
                   companions=want["companions"], memory_mb=entry.get("memory_mb"),
                   lifecycle=LIFECYCLES.get(entry.get("lifecycle"), ""), free_mb=_free_mb(),
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
    row = _row(record, live, data)
    logs = ""
    if supervisor.enabled() and "unreachable" not in live:
        try:
            logs = supervisor.logs(app_id, lines=LOG_LINES)
        except supervisor.SupervisorError as exc:
            logs = f"(logs unavailable: {exc})"
    spec = _spec(app_id)
    job = jobs.get(app_id)
    granted = set(record.granted_permissions or ())
    return _render(request, "hub_settings/app_manage.html", "apps", app=row, record=record,
                   job=job, error=(live.get("error") if live.get("status") == "failed" else ""),
                   logs=logs, settings_url=_settings_url(spec) if spec else "",
                   perms=[{"value": p, "on": p in granted, **describe(p)}
                          for p in record.requested_permissions or ()],
                   summary=_short([p for p in record.requested_permissions or () if p in granted]),
                   network=NETWORK_INFO.get(record.approved_network),
                   review=_review(record, request.GET.get("review"), data))


def _review(record, version, data):
    """What update ``version`` asks for beyond what's approved, for the review card."""
    if not version or data is None:
        return None
    entry = services.update_available(record, data)
    if entry is None or entry["version"] != version:
        return None
    extra = services.widened(record, entry)
    if not extra:
        return None
    return {"version": version, "perms": [describe(p) for p in extra.get("permissions", ())],
            "network": NETWORK_INFO.get(extra.get("network")), "companions": extra.get("companions", ())}



def _back(app_id):
    return redirect("hub_settings_app_manage", app_id)


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
    entry = services.update_available(record)
    if entry is None:
        messages.info(request, "Already up to date.")
        return _back(app_id)
    extra = services.widened(record, entry)
    if extra and request.POST.get("approve") != "1":
        messages.warning(request, "This update asks for more access; review it below.")
        return redirect(reverse("hub_settings_app_manage", args=[app_id]) + "?review=" + entry["version"])
    if not jobs.start(app_id, "Updating", _update, app_id, entry["version"], bool(extra)):
        messages.info(request, "Something is already running for this app.")
    return _back(app_id)


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
    return _back(app_id)


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
    if not jobs.start("*", "Updating all", _update_all):
        messages.info(request, "An update is already running.")
    else:
        messages.success(request, "Updating apps in the background.")
    return redirect("hub_settings_apps")
