"""What hub pages poll (hub/api/clock/): the hub's UTC offset and build id.

Time: clocks on hub pages show the hub's time (Settings > General), not the browser's.
Kiosk browsers ship old time zone data: the Sun Peaks display still applies
Pacific standard time to America/Vancouver after BC moved to permanent UTC-7
(2026-11-01), so its clock ran an hour slow. The server's zoneinfo is current, so
pages get the offset from here and format times in UTC shifted by it.

Build: changes when the hub image or any installed app version changes. Pages
reload themselves when it does, so a wall display picks up an update without
anyone touching it (base.html).
"""
import hashlib
import os
import time
from datetime import datetime, timezone

_BUILD_TTL = 30
_build_cache = {"at": 0.0, "id": ""}


def utc_offset_minutes(now=None):
    """Minutes east of UTC of the hub's time zone at ``now`` (default: now)."""
    from bdh_sdk.location import tzinfo

    try:
        tz = tzinfo()
    except Exception:
        tz = timezone.utc
    offset = (now or datetime.now(timezone.utc)).astimezone(tz).utcoffset()
    return int(offset.total_seconds() // 60)


def build_id():
    """Short fingerprint of the hub image (``HUB_BUILD``) and installed app versions."""
    now = time.monotonic()
    if _build_cache["id"] and now - _build_cache["at"] < _BUILD_TTL:
        return _build_cache["id"]
    parts = [os.environ.get("HUB_BUILD", "")]
    try:
        from broker.models import AppInstall

        parts += [f"{a}={v}" for a, v in AppInstall.objects.order_by("app_id").values_list("app_id", "version")]
    except Exception:
        pass  # no broker tables yet: the image alone
    _build_cache.update(at=now, id=hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12])
    return _build_cache["id"]
