"""The app catalog (blackdiamondlodge/bdh-app-catalog): what can be installed.

Fetched with ``If-None-Match`` and cached on disk (``BDH_CATALOG_CACHE_DIR``),
refreshed when it is over an hour old and on demand (``catalog_refresh``).
A failed or invalid fetch keeps the last good copy.

Versions are listed newest first (PEP 440). :func:`versions` hides what this
hub can't use: yanked versions, newer hub APIs, SDK requirements the hub's
bdh-sdk doesn't meet and, unless the hub follows the beta channel
(``[store] channel = "beta"`` in its site.toml), beta versions
(``"channel": "beta"``, PEP 440 pre-releases until they're promoted).
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
_cache = {"data": None, "etag": "", "fetched": 0.0, "dir": None, "disk_mtime": 0.0}


class CatalogError(Exception):
    pass


def url():
    return getattr(settings, "BDH_CATALOG_URL", "") or DEFAULT_URL


def cache_dir():
    configured = getattr(settings, "BDH_CATALOG_CACHE_DIR", "")
    return Path(configured or os.path.join(settings.MEDIA_ROOT, ".catalog"))


CHANNELS = ("stable", "beta")


def hub_channel():
    """The release channel this hub follows: ``stable`` (default) or ``beta``."""
    value = getattr(settings, "HUB_CHANNEL", "stable")
    return value if value in CHANNELS else "stable"


def is_beta(entry):
    """Whether catalog version ``entry`` is on the beta channel (no channel means stable)."""
    return isinstance(entry, dict) and entry.get("channel") == "beta"


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


def _disk_mtime():
    try:
        return _paths()[0].stat().st_mtime
    except OSError:
        return 0.0


def _ensure_loaded():
    """Fill the in-memory copy from disk (once per cache dir), and again whenever
    another process (the supervisor sync, ``catalog_refresh``) saved a newer copy.

    Compares against the disk mtime this process last saw, not ``fetched``: that
    one is backdated on failures (retry backoff) and must not trigger a reload."""
    root = str(cache_dir())
    if _cache["dir"] != root:
        data, etag, fetched = _load_disk()
        _cache.update(data=data, etag=etag, fetched=fetched, dir=root, disk_mtime=fetched)
        return
    mtime = _disk_mtime()
    if mtime > _cache["disk_mtime"]:
        data, etag, fetched = _load_disk()
        _cache["disk_mtime"] = mtime
        if data is not None:
            _cache.update(data=data, etag=etag, fetched=fetched)


def reset():
    with _lock:
        _cache.update(data=None, etag="", fetched=0.0, dir=None, disk_mtime=0.0)
        _background["thread"] = None


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
            _cache["disk_mtime"] = _disk_mtime()
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
        _cache.update(data=data, etag=etag, fetched=time.time(), disk_mtime=_disk_mtime())
        return "updated"


_background = {"thread": None}


def _refresh_quietly():
    try:
        refresh()
    except CatalogError as exc:
        log.warning("%s", exc)


def get_cached():
    """The cached catalog right away, refreshing it in the background when stale.

    For pages: they shouldn't wait on GitHub. Only the very first fetch (nothing
    cached yet) blocks. Returns ``(data, thread)``; ``thread`` is the refresh
    started, if any (tests join it)."""
    with _lock:
        _ensure_loaded()
        data, stale = _cache["data"], time.time() - _cache["fetched"] >= MAX_AGE
    if data is None:
        return get(), None
    if not stale:
        return data, None
    with _lock:
        running = _background["thread"]
        if running and running.is_alive():
            return data, None
        thread = threading.Thread(target=_refresh_quietly, name="catalog-refresh", daemon=True)
        _background["thread"] = thread
    thread.start()
    return data, thread


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

def compatible(entry, api=None, sdk=None, channel=None):
    """Whether this hub can use catalog version ``entry`` (and why not)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
        return False, "malformed"
    if entry.get("yanked"):
        return False, "yanked"
    if is_beta(entry) and (channel or hub_channel()) != "beta":
        return False, "beta (this hub follows stable)"
    api = hub_api() if api is None else api
    wanted = entry.get("hub_api")
    if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted > api:
        return False, f"needs hub API {wanted}"
    if wanted >= 2 and (entry.get("kind") != "image" or not entry.get("image")):
        return False, "not an image"
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


def display_name(install):
    """What to call an installed app: its first tile's title, else its manifest or
    store name, else its id made readable. Never fetches; uses the cached catalog."""
    manifest = install.manifest if isinstance(install.manifest, dict) else {}
    tiles = manifest.get("tiles")
    if isinstance(tiles, list) and tiles and isinstance(tiles[0], dict):
        title = tiles[0].get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    if isinstance(manifest.get("name"), str) and manifest["name"].strip():
        return manifest["name"].strip()
    try:
        entry = app(install.app_id, get(refresh_stale=False) or {}) or {}
    except Exception:
        log.exception("catalog lookup for %s failed", install.app_id)
        entry = {}
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return install.app_id.replace("_", " ").title()


def versions(app_id, data=None):
    """Usable versions of ``app_id``, newest first (PEP 440)."""
    entry = app(app_id, data)
    if entry is None:
        return []
    usable = [v for v in entry.get("versions") or () if compatible(v)[0]]
    return sorted(usable, key=_version_key, reverse=True)


def _version_key(entry):
    try:
        return (1, Version(entry["version"]))
    except InvalidVersion:
        return (0, Version("0"))


def apps(data=None):
    """``{app_id: app}`` for apps with at least one usable version."""
    data = get() if data is None else data
    return {app_id: entry for app_id, entry in ((data or {}).get("apps") or {}).items()
            if isinstance(entry, dict) and versions(app_id, data)}


def find(app_id, version=None, data=None):
    """The catalog entry for ``app_id`` at ``version`` (newest usable when None).

    With the cached catalog, an app or version it doesn't list yet (published
    since the last hourly fetch) triggers one forced refresh before giving up."""
    if data is not None:
        return _find(app_id, version, data)
    data = get()
    entry = app(app_id, data)
    if entry is None or (version is not None and not any(
            isinstance(v, dict) and v.get("version") == version for v in entry.get("versions") or ())):
        try:
            refresh(force=True)
        except CatalogError as exc:
            log.warning("%s", exc)
        data = get(refresh_stale=False)
    return _find(app_id, version, data)


def _find(app_id, version, data):
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
