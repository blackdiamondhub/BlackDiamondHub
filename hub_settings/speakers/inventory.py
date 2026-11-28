"""The hub's speakers: found on the LAN and in Home Assistant, kept in :class:`Speaker` rows.

Sonos rooms come from :mod:`.sonos` (``sonos:<uid>``). Home Assistant adds
its Alexa speakers (``alexa:<instance>:<entity>``), any other media player
that can announce (``ha:<instance>:<entity>``), and for Sonos the HA entity
(its registry ``unique_id`` is the room's uid), which becomes the room's
``legacy_id`` and its fallback route. HA entities a native driver covers are
not listed twice.

Only this site's speakers are kept: Home Assistant's default (local)
instance alone is asked, and Sonos only from the households local HA knows
(:func:`sonos.topology`). Rows another HA instance or another household
left behind are pruned. Satellites, subs and stereo-pair secondaries are
not rooms: their ids (and HA entities) become ``aliases`` of the room they
play in, so a pick saved with one of them plays on that room.

Refreshed at most every :data:`TTL` seconds when asked for, sooner when an
id isn't known; a source that doesn't answer leaves its rows as they were,
so ids saved by apps keep resolving while HA or the LAN is down.
Announcements (:func:`resolve`) refresh in the background and wait at most
:data:`REQUEST_WAIT` seconds for it: a slow refresh, or one already running,
leaves them the saved rows.
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
REQUEST_WAIT = 10
SONOS_DRIVER, ALEXA_DRIVER, HA_DRIVER = "sonos", "ha_alexa", "ha_media"
TITLES = {"sonos": "Sonos", "alexa": "Alexa", "ha": "Home Assistant"}
PLAY_MEDIA, MEDIA_ANNOUNCE = 512, 1048576
API_CAPS = ("audio", "duck", "text", "volume")

_lock = threading.Lock()
_refreshed = {"at": None}
_bg = {"thread": None}
_bg_lock = threading.Lock()


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
        default, data = _local_ha()
        seen = _refresh_sonos(_local_uids(default, data))
        _refresh_ha(seen, default, data)
        _prune(default)


def _local_ha():
    """``(default instance id, (entity registry, states) or None when it didn't answer)``."""
    from bdh_sdk import ha

    from hub_settings import ha as hub_ha

    try:
        default = hub_ha.default_id()
    except Exception:  # noqa: BLE001 - no instances table yet
        return None, None
    if default is None:
        return None, None
    try:
        (entries,) = ha.ws_commands({"type": "config/entity_registry/list"}, instance=default)
        states = {s["entity_id"]: s for s in ha.get_all_states(instance=default)}
    except Exception as exc:  # noqa: BLE001 - HA down: keep the rows (and their aliases)
        log.info("speakers: Home Assistant %s unavailable: %s", default, exc)
        return default, None
    return default, (entries or (), states)


def _local_uids(default, data):
    """The Sonos uids this site's Home Assistant knows (as last seen, while it's down)."""
    from hub_settings.models import Speaker

    if data is not None:
        return {e.get("unique_id") for e in data[0]
                if e.get("platform") == "sonos" and e.get("unique_id")}
    if default is None:
        return set()
    return {r.split(":", 1)[1] for r in Speaker.objects.filter(kind=Speaker.SONOS, ha_instance=str(default))
            .values_list("native_id", flat=True)}


def _refresh_sonos(local_uids=()):
    """Native Sonos rooms; returns the uids that answered (None when Sonos didn't)."""
    from hub_settings.models import Speaker

    seeds = list(Speaker.objects.filter(kind=Speaker.SONOS).exclude(address="")
                 .order_by("-last_seen").values_list("address", flat=True))
    try:
        rooms = sonos.topology(seeds, local_uids=local_uids)
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
    seen, hidden = set(), {}
    for room in rooms:
        uid = room["uid"]
        seen.add(uid)
        row, _created = Speaker.objects.get_or_create(
            native_id=f"sonos:{uid}", defaults={"kind": Speaker.SONOS, "driver": SONOS_DRIVER, "name": room["name"]})
        clip = row.caps.get("audio_clip", True) if caps is None else sonos.AUDIO_CLIP in caps.get(uid, ())
        row.name, row.address, row.online, row.last_seen = room["name"], room["address"], True, now
        row.household = room.get("household") or row.household
        members = [f"sonos:{h}" for h in room.get("hidden", ())]
        hidden.update(dict.fromkeys(members, row))
        row.aliases = members + [a for a in row.aliases if not a.startswith("sonos:")]
        row.caps = sonos_caps(clip, row.ha_entity)
        row.save()
    for old in Speaker.objects.filter(native_id__in=list(hidden)):
        _fold(old, hidden[old.native_id])
    households = {r.get("household") for r in rooms} - {None, ""}
    if households:  # another site's household, from before the hub kept to its own
        Speaker.objects.filter(kind=Speaker.SONOS).exclude(household="").exclude(household__in=households).delete()
    Speaker.objects.filter(kind=Speaker.SONOS).exclude(native_id__in=[f"sonos:{u}" for u in seen]).update(
        online=False)
    return seen


def _fold(member, room):
    """``member`` (a satellite, sub or pair secondary) had a row of its own: its ids now mean ``room``."""
    room.aliases = list(dict.fromkeys(room.aliases + [i for i in (member.legacy_id,) if i]))
    room.save(update_fields=["aliases"])
    member.delete()


def _name(state, entry, eid):
    return ((state or {}).get("attributes", {}).get("friendly_name") or entry.get("name")
            or entry.get("original_name") or eid.split(".", 1)[1])


def _refresh_ha(native_seen, iid, data):
    """Rows from the default (local) Home Assistant instance's media players."""
    from hub_settings.models import Speaker

    if data is None:
        return
    entries, states = data
    now = timezone.now()
    rooms_of = {a: r for r in Speaker.objects.filter(kind=Speaker.SONOS) for a in r.aliases
                if a.startswith("sonos:")}
    listed = set()
    for e in entries:
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
            room = rooms_of.get(f"sonos:{uid}")
            if room is not None:  # a satellite/sub/pair secondary: its entity means the room
                if eid not in room.aliases:
                    room.aliases = room.aliases + [eid]
                    room.save(update_fields=["aliases"])
                continue
            row, _ = Speaker.objects.get_or_create(
                native_id=f"sonos:{uid}", defaults={"kind": Speaker.SONOS, "driver": SONOS_DRIVER, "name": name})
            row.ha_instance, row.ha_entity, row.legacy_id = str(iid), eid, eid
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
                  "ha_instance": str(iid), "ha_entity": eid, "legacy_id": eid}
        if available:
            fields["last_seen"] = now
        Speaker.objects.update_or_create(native_id=native_id, defaults=fields)
    Speaker.objects.filter(ha_instance=str(iid), kind__in=[Speaker.ALEXA, Speaker.HA]).exclude(
        native_id__in=listed).update(online=False)


def _prune(default):
    """Drop what other Home Assistant instances added (another site's speakers)."""
    from hub_settings.models import Speaker

    if default is None:
        return
    others = Speaker.objects.exclude(ha_instance="").exclude(ha_instance=str(default))
    others.filter(kind__in=[Speaker.ALEXA, Speaker.HA]).delete()
    for row in others.filter(kind=Speaker.SONOS):
        if not row.address:  # never seen on this LAN
            row.delete()
            continue
        if row.legacy_id == row.ha_entity:
            row.legacy_id = None
        row.ha_instance, row.ha_entity = "", ""
        row.caps = sonos_caps(row.caps.get("audio_clip", True), None)
        row.save()


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
    query = Q(native_id__in=ids) | Q(legacy_id__in=ids)
    for i in ids:
        query |= Q(aliases__contains=[i])
    matched = list(Speaker.objects.filter(query).order_by("id"))
    for row in matched:
        found.setdefault(row.native_id, row)
        if row.legacy_id:
            found.setdefault(row.legacy_id, row)
    for row in matched:  # a room's own ids win over another's alias
        for alias in row.aliases:
            found.setdefault(alias, row)
    return {i: found.get(i) for i in ids}


def _due(max_age):
    at = _refreshed["at"]
    return at is None or time.monotonic() - at >= max_age


def _background(max_age):
    from django.db import close_old_connections

    try:
        _safe_refresh(max_age=max_age)
    finally:
        close_old_connections()


def _bounded_refresh(max_age, wait):
    """Refresh if due (or join one running), waiting at most ``wait`` s for it."""
    with _bg_lock:
        thread = _bg["thread"]
        if thread is None or not thread.is_alive():
            if not _due(max_age):
                return
            thread = threading.Thread(target=_background, args=(max_age,), name="speakers-refresh", daemon=True)
            _bg["thread"] = thread
            thread.start()
    if wait > 0:
        thread.join(wait)
    if thread.is_alive():
        log.info("speakers: inventory refresh still running, using the saved speakers")


def resolve(ids, wait=REQUEST_WAIT):
    """``{id: Speaker or None}`` for native or legacy ids, refreshing once for unknown ones.

    Waits at most ``wait`` seconds (capped at :data:`REQUEST_WAIT`) for refreshes.
    """
    end = time.monotonic() + max(0.0, min(wait, REQUEST_WAIT))
    _bounded_refresh(TTL, end - time.monotonic())
    out = _lookup(ids)
    if any(v is None for v in out.values()):
        _bounded_refresh(UNKNOWN_TTL, end - time.monotonic())
        out = _lookup(ids)
    return out


def mark(row, online):
    """Record a live result (the websocket connect is the real online check)."""
    from hub_settings.models import Speaker

    if row.online != online:
        row.online = online
        Speaker.objects.filter(pk=row.pk).update(online=online)
