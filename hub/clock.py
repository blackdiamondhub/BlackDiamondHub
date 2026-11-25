"""The hub's local time for screens whose browser has the wrong time zone rules.

Clocks on hub pages show the hub's time (Settings > General), not the browser's.
Kiosk browsers ship old time zone data: the Sun Peaks display still applies
Pacific standard time to America/Vancouver after BC moved to permanent UTC-7
(2026-11-01), so its clock ran an hour slow. The server's zoneinfo is current, so
pages get the offset from here and format times in UTC shifted by it.
"""
from datetime import datetime, timezone


def utc_offset_minutes(now=None):
    """Minutes east of UTC of the hub's time zone at ``now`` (default: now)."""
    from bdh_sdk.location import tzinfo

    try:
        tz = tzinfo()
    except Exception:
        tz = timezone.utc
    offset = (now or datetime.now(timezone.utc)).astimezone(tz).utcoffset()
    return int(offset.total_seconds() // 60)
