"""The hub's speakers: found on the LAN and in Home Assistant, kept in :class:`Speaker` rows.

Sonos rooms come from :mod:`.sonos` (``sonos:<uid>``). Home Assistant adds
its Alexa speakers (``alexa:<instance>:<entity>``), any other media player
that can announce (``ha:<instance>:<entity>``), and for Sonos the HA entity
(its registry ``unique_id`` is the room's uid), which becomes the room's
``legacy_id`` and its fallback route. HA entities a native driver covers are
not listed twice.

Refreshed at most every :data:`TTL` seconds when asked for, sooner when an
id isn't known; a source that doesn't answer leaves its rows as they were,
so ids saved by apps keep resolving while HA or the LAN is down.
"""
import logging
import threading
import time

from django.db.models import Q
from django.utils import timezone

from . import loop, sonos

log = logging.getLogger(__name__)

TTL = 300
UNKNOWN_TTL = 30
SONOS_DRIVER, ALEXA_DRIVER, HA_DRIVER = "sonos", "ha_alexa", "ha_media"
TITLES = {"sonos": "Sonos", "alexa": "Alexa", "ha": "Home Assistant"}
PLAY_MEDIA, MEDIA_ANNOUNCE = 512, 1048576
API_CAPS = ("audio", "duck", "text", "volume")

_lock = threading.Lock()
_refreshed = {"at": None}


def reset():
    with _lock:
        _refreshed["at"] = None


def sonos_caps(audio_clip, ha_alias):
    audio = bool(audio_clip or ha_alias)
    return {"audio": audio, "duck": audio, "text": False, "volume": audio, "audio_clip": bool(audio_clip)}


ALEXA_CAPS = {"audio": False, "duck": False, "text": True, "volume": False}
HA_CAPS = {"audio": True, "duck": True, "text": False, "volume": False}


def refresh(force=False, max_age=TTL):
    """Update the rows from Sonos and Home Assistant unless done within ``max_age`` s."""
    with _lock:
        at = _refreshed["at"]
        if not force and at is not None and time.monotonic() - at < max_age:
            return
        _refreshed["at"] = time.monotonic()
        seen = _refresh_sonos()
        _refresh_ha(seen)


def _refresh_sonos():
    """Native Sonos rooms; returns the uids that answered (None when Sonos didn't)."""
    from hub_settings.models import Speaker

    seeds = list(Speaker.objects.filter(kind=Speaker.SONOS).exclude(address="")
                 .order_by("-last_seen").values_list("address", flat=True))
    try:
        rooms = sonos.topology(seeds)
    except Exception:  # noqa: BLE001 - never let discovery break an announcement
        log.warning("speakers: Sonos topology failed", exc_info=True)
        rooms = None
    if not rooms:
        return None
    try:
        caps = loop.run(sonos.capabilities(rooms[0]["address"]), timeout=sonos.CONNECT_TIMEOUT + 8)
    except Exception as exc:  # noqa: BLE001 - keep what we knew
        log.info("speakers: couldn't read Sonos capabilities: %s", exc)
        caps = None
    now = timezone.now()
    seen = set()
    for room in rooms:
        uid = room["uid"]
        seen.add(uid)
        row, _created = Speaker.objects.get_or_create(
            native_id=f"sonos:{uid}", defaults={"kind": Speaker.SONOS, "driver": SONOS_DRIVER, "name": room["name"]})
        clip = row.caps.get("audio_clip", True) if caps is None else sonos.AUDIO_CLIP in caps.get(uid, ())
        row.name, row.address, row.online, row.last_seen = room["name"], room["address"], True, now
        row.caps = sonos_caps(clip, row.ha_entity)
        row.save()
    Speaker.objects.filter(kind=Speaker.SONOS).exclude(native_id__in=[f"sonos:{u}" for u in seen]).update(
        online=False)
    return seen


def _name(state, entry, eid):
    return ((state or {}).get("attributes", {}).get("friendly_name") or entry.get("name")
            or entry.get("original_name") or eid.split(".", 1)[1])


def _refresh_ha(native_seen):
    from bdh_sdk import ha

    from hub_settings import ha as hub_ha
    from hub_settings.models import Speaker

    try:
        instances = hub_ha.instances()
    except Exception:  # noqa: BLE001 - no instances table yet
        return
    default = next((i["id"] for i in instances if i["default"]), None)
    now = timezone.now()
    for inst in instances:
        iid = inst["id"]
        try:
            (entries,) = ha.ws_commands({"type": "config/entity_registry/list"}, instance=iid)
            states = {s["entity_id"]: s for s in ha.get_all_states(instance=iid)}
        except Exception as exc:  # noqa: BLE001 - HA down: keep the rows (and their aliases)
            log.info("speakers: Home Assistant %s unavailable: %s", iid, exc)
            continue
        alias = (lambda eid: eid) if iid == default else (lambda eid: None)
        listed = set()
        for e in entries or ():
            eid = e.get("entity_id") or ""
            if not eid.startswith("media_player.") or e.get("disabled_by"):
                continue
            state = states.get(eid)
            if state is None:
                continue
            available = state.get("state") != "unavailable"
            name = _name(state, e, eid)
            platform = e.get("platform")
            if platform == "sonos":
                uid = e.get("unique_id")
                if not uid:
                    continue
                row, _ = Speaker.objects.get_or_create(
                    native_id=f"sonos:{uid}", defaults={"kind": Speaker.SONOS, "driver": SONOS_DRIVER, "name": name})
                if row.ha_instance and row.ha_instance != iid and row.ha_instance == default:
                    continue  # the default instance's entity wins
                row.ha_instance, row.ha_entity = iid, eid
                if alias(eid):
                    row.legacy_id = eid
                if native_seen is None or uid not in native_seen:
                    row.online = available or (native_seen is None and row.online and bool(row.address))
                    if available:
                        row.last_seen = now
                row.caps = sonos_caps(row.caps.get("audio_clip", bool(row.address)), eid)
                row.save()
                continue
            if platform == "alexa_media":
                kind, driver, caps = Speaker.ALEXA, ALEXA_DRIVER, ALEXA_CAPS
            else:
                features = (state.get("attributes") or {}).get("supported_features") or 0
                if not (int(features) & PLAY_MEDIA and int(features) & MEDIA_ANNOUNCE):
                    continue
                kind, driver, caps = Speaker.HA, HA_DRIVER, HA_CAPS
            native_id = f"{kind}:{iid}:{eid}"
            listed.add(native_id)
            fields = {"kind": kind, "driver": driver, "name": name, "caps": dict(caps), "online": available,
                      "ha_instance": iid, "ha_entity": eid}
            if available:
                fields["last_seen"] = now
            if alias(eid):
                fields["legacy_id"] = eid
            Speaker.objects.update_or_create(native_id=native_id, defaults=fields)
        Speaker.objects.filter(ha_instance=iid, kind__in=[Speaker.ALEXA, Speaker.HA]).exclude(
            native_id__in=listed).update(online=False)


def _safe_refresh(**kwargs):
    try:
        refresh(**kwargs)
    except Exception:  # noqa: BLE001 - the saved rows still answer
        log.warning("speakers: refresh failed", exc_info=True)


def rows():
    from hub_settings.models import Speaker

    _safe_refresh()
    return list(Speaker.objects.all())


def label(row):
    out = f"{row.name} ({TITLES.get(row.kind, row.kind)})"
    return out if row.online else out + " - offline"


def public_id(row):
    return row.legacy_id or row.native_id


def pairs():
    """``[(legacy id or native id, "Name (Sonos)")]`` by label, for ``list_speakers``."""
    return sorted(((public_id(r), label(r)) for r in rows()), key=lambda p: p[1].lower())


def device(row):
    return {"id": row.native_id, "legacy_id": row.legacy_id, "name": row.name, "kind": row.kind,
            "caps": {k: bool(row.caps.get(k)) for k in API_CAPS}, "online": row.online}


def devices():
    return [device(r) for r in sorted(rows(), key=lambda r: (r.name.lower(), r.native_id))]


def _lookup(ids):
    from hub_settings.models import Speaker

    found = {}
    for row in Speaker.objects.filter(Q(native_id__in=ids) | Q(legacy_id__in=ids)).order_by("id"):
        found.setdefault(row.native_id, row)
        if row.legacy_id:
            found.setdefault(row.legacy_id, row)
    return {i: found.get(i) for i in ids}


def resolve(ids):
    """``{id: Speaker or None}`` for native or legacy ids, refreshing once for unknown ones."""
    _safe_refresh()
    out = _lookup(ids)
    if any(v is None for v in out.values()):
        _safe_refresh(max_age=UNKNOWN_TTL)
        out = _lookup(ids)
    return out


def mark(row, online):
    """Record a live result (the websocket connect is the real online check)."""
    from hub_settings.models import Speaker

    if row.online != online:
        row.online = online
        Speaker.objects.filter(pk=row.pk).update(online=online)
