"""Installing, upgrading and removing container apps.

The catalog says what exists, bdh-supervisor runs it and :class:`AppInstall`
is the hub's record: the version, the token generation, the public port (whose
index is the supervisor's slot) and what the admin approved. An update that
asks for more than was approved (permissions, network, companions) raises
:class:`ReapprovalRequired` until it is approved.
"""
import logging
import re
import time

from django.conf import settings
from django.db import transaction

from . import catalog, remote, supervisor
from .models import AppInstall
from .tokens import token_for

log = logging.getLogger(__name__)

NETWORKS = ("none", "outbound", "lan")  # least to most privileged
INSTALL_TIMEOUT = 900.0
POLL = 2.0
TZ_RE = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")
BRAND_KEYS = ("name", "subtitle")

# Tests replace this.
sleep = time.sleep


class ServiceError(Exception):
    pass


class ReapprovalRequired(ServiceError):
    """The new version wants more than the admin approved; ``widened`` says what."""

    def __init__(self, app_id, version, widened):
        self.widened = widened
        parts = []
        if widened.get("permissions"):
            parts.append("permissions " + ", ".join(widened["permissions"]))
        if widened.get("network"):
            parts.append(f"network {widened['network']}")
        if widened.get("companions"):
            parts.append("companions " + ", ".join(widened["companions"]))
        super().__init__(f"{app_id} {version} needs approval for: {'; '.join(parts)}")


# --- what the catalog asks for ------------------------------------------------

def _strings(value):
    return sorted({v for v in value or () if isinstance(v, str)})


def requested(entry):
    """What catalog version ``entry`` asks for."""
    network = entry.get("network")
    return {
        "permissions": _strings(entry.get("permissions")),
        "network": network if network in NETWORKS else "none",
        "companions": _strings(entry.get("companions")),
    }


def _rank(network):
    return NETWORKS.index(network) if network in NETWORKS else 0


def declined(install):
    """Permissions an earlier version asked for and the admin left off."""
    return set(install.requested_permissions or ()) - set(install.granted_permissions or ())


def widened(install, entry):
    """What ``entry`` asks for beyond ``install``'s approval (empty when nothing).

    Permissions the admin already declined don't count: they stay off without
    asking again."""
    want = requested(entry)
    out = {}
    perms = sorted(set(want["permissions"]) - set(install.granted_permissions or ()) - declined(install))
    if perms:
        out["permissions"] = perms
    if _rank(want["network"]) > _rank(install.approved_network):
        out["network"] = want["network"]
    companions = sorted(set(want["companions"]) - set(install.approved_companions or ()))
    if companions:
        out["companions"] = companions
    return out


# --- what the hub tells the supervisor ---------------------------------------

def time_zone():
    try:
        from bdh_sdk.location import location

        where = location()
        tz = where.timezone if where else ""
    except Exception:
        tz = ""
    tz = tz or getattr(settings, "TIME_ZONE", "UTC") or "UTC"
    return tz if TZ_RE.match(tz) else "UTC"


def hub_brand():
    brand = getattr(settings, "HUB_BRAND", None) or {}
    return {k: brand[k] for k in BRAND_KEYS if isinstance(brand.get(k), str)}


def _local_ids():
    return {spec.id for spec in getattr(settings, "HUB_PLUGINS", ())}


def _put(install):
    return supervisor.put_app(
        install.app_id, version=install.version, token=token_for(install), slot=install.slot,
        network=install.approved_network or "none", allow_lan=install.allow_lan,
        time_zone=time_zone(), hub_brand=hub_brand())


def wait_installed(app_id, timeout=INSTALL_TIMEOUT):
    """Poll the supervisor until ``app_id`` is installed; returns its view."""
    deadline = time.monotonic() + timeout
    while True:
        view = supervisor.get_app(app_id)
        if view.get("status") == "installed":
            return view
        if view.get("status") == "failed":
            raise ServiceError(f"{app_id} install failed: {view.get('error') or 'unknown error'}")
        if time.monotonic() >= deadline:
            raise ServiceError(f"{app_id} is still {view.get('status')} after {int(timeout)}s")
        sleep(POLL)


def _apply_view(install, view):
    fields = {"upstream": view.get("upstream") or "", "enabled": True}
    for name in ("network", "lifecycle"):
        if isinstance(view.get(name), str):
            fields[name] = view[name]
    if isinstance(view.get("memory_mb"), int):
        fields["memory_mb"] = view["memory_mb"]
    for name, value in fields.items():
        setattr(install, name, value)
    install.save()


def _load_manifest(install):
    """Start the app and fetch its manifest (its tiles, settings and permissions)."""
    try:
        supervisor.start(install.app_id, wait=True)
        remote.fetch_manifest(install)
    except (supervisor.SupervisorError, remote.RemoteError) as exc:
        log.warning("%s: manifest not fetched yet: %s", install.app_id, exc)
        return False
    finally:
        remote.installs_changed()
    return True


def _entry(app_id, version):
    entry = catalog.find(app_id, version)
    api = catalog.hub_api()
    if entry.get("hub_api") != api:
        raise ServiceError(f"{app_id} {entry['version']} is a hub API {entry.get('hub_api')} app; "
                           f"container apps need hub API {api}")
    return entry


# --- the operations -----------------------------------------------------------

def _narrow(asked, permissions):
    """``asked`` limited to ``permissions`` (all of ``asked`` when that is None)."""
    if permissions is None:
        return list(asked)
    keep = set(_strings(permissions))
    return [p for p in asked if p in keep]


def install(app_id, version=None, *, network=None, allow_lan=False, approve=False, permissions=None,
            wait=True, timeout=INSTALL_TIMEOUT):
    """Install ``app_id`` from the catalog, or update it when it is installed.

    A fresh install approves what the version asks for, narrowed by
    ``network`` and ``permissions``. An update that asks for more needs
    ``approve``.
    """
    if network is not None and network not in NETWORKS:
        raise ServiceError(f"network must be one of {', '.join(NETWORKS)}")
    if app_id in _local_ids():
        raise ServiceError(f"{app_id} runs in-process on this hub; set runtime = \"container\" "
                           f"in its [apps.{app_id}] site table to run it in a container")
    existing = AppInstall.objects.filter(app_id=app_id).first()
    if existing is not None and not existing.is_container:
        raise ServiceError(f"{app_id} is an external app; remove it with broker_app --remove first")
    if not supervisor.enabled():
        raise supervisor.SupervisorDisabled("the supervisor isn't configured on this hub")
    entry = _entry(app_id, version)
    if existing is not None:
        return upgrade(existing, entry, network=network, allow_lan=allow_lan, approve=approve,
                       permissions=permissions, wait=wait, timeout=timeout)
    want = requested(entry)
    approved = want["network"] if network is None else network
    with transaction.atomic():
        key = AppInstall.cookie_key(app_id)
        clash = next((i.app_id for i in AppInstall.objects.select_for_update()
                      if AppInstall.cookie_key(i.app_id) == key), None)
        if clash:
            raise ServiceError(f"{app_id} clashes with installed app {clash}")
        record = AppInstall(
            app_id=app_id, runtime=AppInstall.CONTAINER, version=entry["version"], enabled=False,
            requested_permissions=want["permissions"],
            granted_permissions=_narrow(want["permissions"], permissions),
            approved_network=approved, allow_lan=bool(allow_lan), approved_companions=want["companions"],
            network=want["network"], lifecycle=entry.get("lifecycle") or "",
            memory_mb=entry.get("memory_mb") if isinstance(entry.get("memory_mb"), int) else None)
        record.full_clean(exclude=["port"])
        record.save()
    try:
        _put(record)
    except supervisor.SupervisorError:
        record.delete()
        raise
    if wait:
        _finish(record, timeout)
    return record


def _finish(record, timeout):
    view = wait_installed(record.app_id, timeout)
    _apply_view(record, view)
    _load_manifest(record)


def upgrade(record, entry, *, network=None, allow_lan=None, approve=False, permissions=None,
            wait=True, timeout=INSTALL_TIMEOUT):
    extra = widened(record, entry)
    if extra and not approve:
        raise ReapprovalRequired(record.app_id, entry["version"], extra)
    want = requested(entry)
    if approve:
        if permissions is None:
            skip = declined(record)
            record.granted_permissions = [p for p in want["permissions"] if p not in skip]
        record.approved_companions = want["companions"]
        record.approved_network = want["network"]
    if permissions is not None:
        # Narrowing never needs approval; widening only comes with it.
        allowed = want["permissions"] if approve else record.granted_permissions or ()
        record.granted_permissions = _narrow([p for p in want["permissions"] if p in allowed], permissions)
    if network is not None:
        record.approved_network = network
    if allow_lan is not None:
        record.allow_lan = bool(allow_lan)
    changed = record.version != entry["version"]
    record.version = entry["version"]
    record.requested_permissions = want["permissions"]
    record.granted_permissions = [p for p in record.granted_permissions or () if p in want["permissions"]]
    if changed:
        record.manifest = None
        record.manifest_fetched_at = None
    record.save()
    _put(record)
    if wait:
        _finish(record, timeout)
    return record


def set_permissions(app_id, permissions):
    """Grant ``app_id`` exactly ``permissions`` (only ones it asks for)."""
    record = AppInstall.objects.get(app_id=app_id)
    record.granted_permissions = _narrow(record.requested_permissions or [], permissions)
    record.save(update_fields=["granted_permissions", "updated"])
    remote.installs_changed()
    return record


def finish(app_id, timeout=INSTALL_TIMEOUT):
    """Finish an install or update started with ``wait=False``."""
    record = AppInstall.objects.get(app_id=app_id)
    _finish(record, timeout)
    return record


def update_available(record, data=None):
    """The newest compatible catalog version newer than ``record``'s, or None."""
    if not record.is_container:
        return None
    api = catalog.hub_api()
    usable = [v for v in catalog.versions(record.app_id, data) if v.get("hub_api") == api]
    if not usable:
        return None
    newest = usable[0]
    return newest if _newer(newest["version"], record.version or "") else None


def _newer(a, b):
    def key(v):
        return [int(x) if x.isdigit() else -1 for x in re.split(r"[.+-]", v)]
    try:
        return key(a) > key(b)
    except Exception:
        return a != b


def pending_updates(data=None, *, only_auto=False):
    """``[(record, entry)]`` for installed container apps with a newer usable version."""
    out = []
    for record in AppInstall.objects.filter(runtime=AppInstall.CONTAINER).order_by("app_id"):
        if only_auto and not record.auto_update:
            continue
        entry = update_available(record, data)
        if entry is not None:
            out.append((record, entry))
    return out


def update_all(*, only_auto=False, timeout=INSTALL_TIMEOUT):
    """Update every app with a newer version that needs no new approval.

    Returns ``{app_id: "updated <v>" | "needs approval" | "failed: ..."}``.
    """
    results = {}
    for record, entry in pending_updates(only_auto=only_auto):
        try:
            upgrade(record, entry, timeout=timeout)
            results[record.app_id] = f"updated {entry['version']}"
        except ReapprovalRequired:
            results[record.app_id] = "needs approval"
        except (ServiceError, catalog.CatalogError, supervisor.SupervisorError) as exc:
            log.warning("%s: update to %s failed: %s", record.app_id, entry["version"], exc)
            results[record.app_id] = f"failed: {exc}"
    return results


def uninstall(app_id, purge=False):
    """Remove ``app_id``: its container (and with ``purge`` its data) and its record."""
    record = AppInstall.objects.filter(app_id=app_id).first()
    result = {"deleted": False, "purged_data": False}
    if record is None or record.is_container:
        # With no record it may still be a container the supervisor knows of.
        try:
            result = supervisor.delete_app(app_id, purge_data=purge)
        except (supervisor.NotFound, supervisor.SupervisorDisabled):
            if record is None:
                raise ServiceError(f"{app_id} isn't installed") from None
    if record is not None:
        record.delete()
    return result


def rotate_token(app_id):
    """Give ``app_id`` a new token (the supervisor recreates its container)."""
    record = AppInstall.objects.get(app_id=app_id)
    record.generation += 1
    record.save()
    if record.is_container:
        _put(record)
    return record


def resync(record):
    """Send ``record`` to the supervisor again and take its upstream back."""
    if not record.is_container:
        return None
    view = _put(record)
    if view.get("upstream") and view["upstream"] != record.upstream:
        record.upstream = view["upstream"]
        record.save(update_fields=["upstream", "updated"])
    return view
