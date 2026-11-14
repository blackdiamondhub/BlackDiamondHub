"""The app catalog (blackdiamondlodge/bdh-app-catalog): what can be installed.

Fetched with ``If-None-Match`` and cached on disk (``BDH_CATALOG_CACHE_DIR``),
refreshed when it is over an hour old and on demand (``catalog_refresh``).
A failed or invalid fetch keeps the last good copy.

Versions are listed newest first. :func:`versions` hides what this hub can't
use: yanked versions, newer hub APIs and SDK requirements the hub's bdh-sdk
doesn't meet.
"""
import json
import logging
import os
import threading
import time
from importlib.metadata import PackageNotFoundError, version as dist_version
from pathlib import Path

import httpx
from django.conf import settings
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)

SCHEMA = 1
MAX_AGE = 3600.0
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_BYTES = 4 * 1024 * 1024
DEFAULT_URL = "https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json"

# Tests swap in an httpx.MockTransport.
transport = None

_lock = threading.Lock()
_cache = {"data": None, "etag": "", "fetched": 0.0, "dir": None}


class CatalogError(Exception):
    pass


def url():
    return getattr(settings, "BDH_CATALOG_URL", "") or DEFAULT_URL


def cache_dir():
    configured = getattr(settings, "BDH_CATALOG_CACHE_DIR", "")
    return Path(configured or os.path.join(settings.MEDIA_ROOT, ".catalog"))


def hub_api():
    from bdh_sdk.plugins import API_VERSION

    return API_VERSION


def sdk_version():
    try:
        return dist_version("bdh-sdk")
    except PackageNotFoundError:
        return ""


def validate(data):
    """``data`` if it is a catalog this hub understands, else CatalogError."""
    if not isinstance(data, dict):
        raise CatalogError("catalog is not an object")
    if data.get("schema") != SCHEMA:
        raise CatalogError(f"unknown catalog schema {data.get('schema')!r} (this hub reads {SCHEMA})")
    apps = data.get("apps")
    if not isinstance(apps, dict):
        raise CatalogError("catalog has no apps")
    for app_id, app in apps.items():
        if not isinstance(app, dict) or not isinstance(app.get("versions"), list):
            raise CatalogError(f"catalog app {app_id!r} is malformed")
    return data


# --- the cache ---------------------------------------------------------------

def _paths():
    root = cache_dir()
    return root / "catalog.json", root / "etag"


def _load_disk():
    data_path, etag_path = _paths()
    try:
        data = validate(json.loads(data_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, CatalogError):
        return None, "", 0.0
    try:
        etag = etag_path.read_text(encoding="utf-8").strip()
    except OSError:
        etag = ""
    return data, etag, data_path.stat().st_mtime


def _save_disk(raw, etag):
    data_path, etag_path = _paths()
    try:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = data_path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, data_path)
        etag_path.write_text(etag, encoding="utf-8")
    except OSError as exc:
        log.warning("catalog cache not saved: %s", exc)


def _touch_disk():
    try:
        os.utime(_paths()[0])
    except OSError:
        pass


def _ensure_loaded():
    """Fill the in-memory copy from disk (once per cache dir)."""
    root = str(cache_dir())
    if _cache["dir"] != root:
        data, etag, fetched = _load_disk()
        _cache.update(data=data, etag=etag, fetched=fetched, dir=root)


def reset():
    with _lock:
        _cache.update(data=None, etag="", fetched=0.0, dir=None)


def refresh(force=False):
    """Fetch the catalog if it is stale (or ``force``). Returns ``"updated"``,
    ``"unchanged"`` or ``"fresh"``; raises CatalogError (the cached copy stays)."""
    with _lock:
        _ensure_loaded()
        if not force and _cache["data"] is not None and time.time() - _cache["fetched"] < MAX_AGE:
            return "fresh"
        headers = {"Accept": "application/json"}
        if _cache["etag"] and _cache["data"] is not None:
            headers["If-None-Match"] = _cache["etag"]
        try:
            with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=True) as client:
                resp = client.get(url(), headers=headers)
        except httpx.HTTPError as exc:
            _cache["fetched"] = time.time() - MAX_AGE + 300  # retry in 5 minutes, not every call
            raise CatalogError(f"catalog fetch failed: {exc}") from exc
        if resp.status_code == 304 and _cache["data"] is not None:
            _cache["fetched"] = time.time()
            _touch_disk()
            return "unchanged"
        if resp.status_code != 200:
            _cache["fetched"] = time.time() - MAX_AGE + 300
            raise CatalogError(f"catalog fetch failed: HTTP {resp.status_code}")
        raw = resp.content
        if len(raw) > MAX_BYTES:
            raise CatalogError("catalog is too large")
        try:
            data = validate(json.loads(raw))
        except ValueError as exc:
            _cache["fetched"] = time.time() - MAX_AGE + 300
            raise CatalogError(f"catalog is not JSON: {exc}") from exc
        except CatalogError:
            _cache["fetched"] = time.time() - MAX_AGE + 300
            raise
        etag = resp.headers.get("etag", "")
        _save_disk(raw, etag)
        _cache.update(data=data, etag=etag, fetched=time.time())
        return "updated"


def get(refresh_stale=True):
    """The catalog (refreshing it first when stale); None if never fetched."""
    if refresh_stale:
        try:
            refresh()
        except CatalogError as exc:
            log.warning("%s", exc)
    with _lock:
        _ensure_loaded()
        return _cache["data"]


# --- reading it ----------------------------------------------------------------

def compatible(entry, api=None, sdk=None):
    """Whether this hub can use catalog version ``entry`` (and why not)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
        return False, "malformed"
    if entry.get("yanked"):
        return False, "yanked"
    api = hub_api() if api is None else api
    wanted = entry.get("hub_api")
    if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted > api:
        return False, f"needs hub API {wanted}"
    spec = entry.get("requires_sdk") or ""
    if spec:
        sdk = sdk_version() if sdk is None else sdk
        try:
            if not sdk or Version(sdk) not in SpecifierSet(spec):
                return False, f"needs bdh-sdk {spec}"
        except (InvalidSpecifier, InvalidVersion):
            return False, f"bad requires_sdk {spec!r}"
    return True, ""


def app(app_id, data=None):
    data = get() if data is None else data
    entry = ((data or {}).get("apps") or {}).get(app_id)
    return entry if isinstance(entry, dict) else None


def versions(app_id, data=None):
    """Usable versions of ``app_id``, newest first."""
    entry = app(app_id, data)
    if entry is None:
        return []
    return [v for v in entry.get("versions") or () if compatible(v)[0]]


def apps(data=None):
    """``{app_id: app}`` for apps with at least one usable version."""
    data = get() if data is None else data
    return {app_id: entry for app_id, entry in ((data or {}).get("apps") or {}).items()
            if isinstance(entry, dict) and versions(app_id, data)}


def find(app_id, version=None, data=None):
    """The catalog entry for ``app_id`` at ``version`` (newest usable when None)."""
    data = get() if data is None else data
    if app(app_id, data) is None:
        raise CatalogError(f"{app_id} isn't in the catalog")
    usable = versions(app_id, data)
    if version is None:
        if not usable:
            raise CatalogError(f"{app_id} has no version this hub can run")
        return usable[0]
    for entry in app(app_id, data).get("versions") or ():
        if isinstance(entry, dict) and entry.get("version") == version:
            ok, why = compatible(entry)
            if not ok:
                raise CatalogError(f"{app_id} {version}: {why}")
            return entry
    raise CatalogError(f"{app_id} has no version {version}")
