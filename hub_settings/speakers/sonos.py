"""Sonos, directly: soco for the room list, the local websocket API for clips.

Rooms come from the household topology (``ZoneGroupState``), asked of any
speaker the hub already knows; only when none of them is in this site's
household does the hub run SSDP discovery, bound to its LAN address. Only
the hub's own households are used: those with a player its local Home
Assistant knows, else (no such player answered) those with a player on the
hub's subnet, so another site's Sonos reached over a routed link never
shows up. Satellites, subs and the second half of a stereo pair are not
rooms: each is listed under the room it plays in (``hidden``), whose uid
(the soundbar, or the pair's primary) is the one clips go to.

Clips use ``audioClip:1 loadAudioClip`` over the speaker's websocket (what
Home Assistant's Sonos announce uses): the speaker ducks what it's playing,
plays the clip and resumes by itself, so nothing is snapshotted or
restored. The command is sent once; ``sonos-websocket``'s own retry would
play a slow speaker's clip twice.
"""
import asyncio
import ipaddress
import logging
import socket

from . import loop

log = logging.getLogger(__name__)

APP_ID = "com.blackdiamondhub.hub"
CLIP_NAME = "Black Diamond Hub"
CONNECT_TIMEOUT = 3
REPLY_TIMEOUT = 6
DISCOVER_TIMEOUT = 3
AUDIO_CLIP = "AUDIO_CLIP"


class NotSent(Exception):
    """The clip never reached the speaker (it can be retried another way)."""


def lan_address(peer=None):
    """The hub's address toward ``peer`` (its default route when None)."""
    from hub.mdns import lan_address as default

    if not peer:
        return default()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 1400))  # UDP: nothing is sent
        return s.getsockname()[0]
    except OSError:
        return default()
    finally:
        s.close()


def local_network(address=None, routes="/proc/net/route"):
    """The on-link network holding the hub's LAN ``address`` (a /24 around it if unknown)."""
    address = address or lan_address()
    ip = ipaddress.IPv4Address(address)
    try:
        with open(routes) as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) < 8 or fields[2] != "00000000":
                    continue  # via a gateway: not on this LAN
                dest, mask = (ipaddress.IPv4Address(bytes.fromhex(fields[i])[::-1]) for i in (1, 7))
                if int(mask) and (int(ip) & int(mask)) == int(dest):
                    return ipaddress.IPv4Network(f"{dest}/{mask}")
    except (OSError, ValueError, StopIteration):
        pass
    return ipaddress.IPv4Network(f"{address}/24", strict=False)


def _owner(zone, visible):
    """The visible room an invisible member (satellite, sub, pair secondary) plays in."""
    maps = ";".join(m for m in (getattr(zone, "_ht_sat_chan_map", None), getattr(zone, "_channel_map", None)) if m)
    for part in maps.split(";"):
        uid = part.split(":", 1)[0]
        if uid in visible:
            return uid
    group = getattr(zone, "group", None)
    coordinator = getattr(group, "coordinator", None)
    return coordinator.uid if coordinator is not None and coordinator.uid in visible else None


def household(zone):
    """``{"id", "rooms": [{uid, name, address, household, hidden}], "members": {uid: address}}``."""
    hid = zone.household_id
    zones = list(zone.all_zones)
    visible = {z.uid: z for z in zones if z.is_visible}
    rooms = {uid: {"uid": uid, "name": z.player_name or uid, "address": z.ip_address, "household": hid,
                   "hidden": []} for uid, z in visible.items()}
    for z in zones:
        if z.uid not in visible:
            owner = _owner(z, visible)
            if owner:
                rooms[owner]["hidden"].append(z.uid)
    for room in rooms.values():
        room["hidden"].sort()
    return {"id": hid, "rooms": sorted(rooms.values(), key=lambda r: r["uid"]),
            "members": {z.uid: z.ip_address for z in zones}}


def _local(households, local_uids, network):
    """The households of this site: any with a player local HA knows, else any on ``network``."""
    known = [h for h in households if local_uids & set(h["members"])]
    if known:
        return known

    def on_lan(address):
        try:
            return ipaddress.IPv4Address(address) in network
        except ValueError:
            return False
    return [h for h in households if any(on_lan(a) for a in h["members"].values())]


def topology(seeds=(), local_uids=()):
    """``[{uid, name, address, household, hidden}]`` of this site's rooms, or None when no Sonos answered.

    Asks the ``seeds`` (last-known addresses) first, then SSDP discovery.
    ``local_uids`` are the players this site's Home Assistant knows.
    Blocking.
    """
    import soco

    local_uids, network = set(local_uids), local_network()
    households, members = {}, set()

    def visit(zone):
        if zone.uid in members:
            return
        found = household(zone)
        households.setdefault(found["id"], found)
        members.update(found["members"])

    for address in dict.fromkeys(a for a in seeds if a):
        try:
            visit(soco.SoCo(address))
        except Exception as exc:  # noqa: BLE001 - moved, off or not a Sonos any more
            log.debug("speakers: Sonos %s didn't answer: %s", address, exc)
    if not _local(households.values(), local_uids, network):
        try:
            found = soco.discover(timeout=DISCOVER_TIMEOUT, interface_addr=lan_address(),
                                  allow_network_scan=False) or ()
        except Exception as exc:  # noqa: BLE001 - no LAN, multicast blocked
            log.warning("speakers: Sonos discovery failed: %s", exc)
            found = ()
        for zone in sorted(found, key=lambda z: z.ip_address):
            try:
                visit(zone)
            except Exception as exc:  # noqa: BLE001
                log.debug("speakers: Sonos %s didn't answer: %s", zone.ip_address, exc)
    if not households:
        return None
    local = _local(households.values(), local_uids, network)
    others = [h["id"] for h in households.values() if h not in local]
    if others:
        log.info("speakers: ignoring other sites' Sonos households %s", ", ".join(others))
    return [room for h in local for room in h["rooms"]]


def _ws(address):
    from sonos_websocket import SonosWebsocket

    async def make():
        return SonosWebsocket(address, session=await loop.session(), timeout=CONNECT_TIMEOUT)
    return make()


async def capabilities(address):
    """``{uid: [capability, ...]}`` for every player in the household, via one speaker."""
    ws = await _ws(address)
    try:
        header, body = await ws.get_groups()
        if not header.get("success", True):
            return {}
        return {p["id"]: list(p.get("capabilities") or []) for p in body.get("players") or []}
    finally:
        await ws.close()


async def _reply(ws, name, timeout):
    """The reply to command ``name``, skipping any events the speaker sends first."""
    from aiohttp import WSMsgType

    async with asyncio.timeout(timeout):
        while True:
            msg = await ws.ws.receive()
            if msg.type != WSMsgType.TEXT:
                raise ConnectionError(f"websocket {msg.type.name.lower()}")
            header, body = msg.json()
            if header.get("response") == name or header.get("success") is False:
                return header, body


async def play_clip(uid, address, url, volume=None):
    """Play ``url`` on the room ``uid`` at ``address``.

    Raises NotSent when the speaker can't be reached (nothing played), any
    other exception when it was sent but not confirmed.
    """
    from sonos_websocket.exception import SonosWebsocketError

    ws = await _ws(address)
    try:
        try:
            await ws.connect()
        except SonosWebsocketError as exc:
            raise NotSent(str(exc)) from None
        options = {"name": CLIP_NAME, "appId": APP_ID, "streamUrl": url}
        if volume is not None:
            options["volume"] = max(0, min(100, int(volume)))
        command = {"namespace": "audioClip:1", "command": "loadAudioClip", "playerId": uid}
        try:
            await ws.ws.send_json([command, options])
        except (ConnectionError, RuntimeError) as exc:
            raise NotSent(str(exc)) from None
        header, body = await _reply(ws, "loadAudioClip", REPLY_TIMEOUT)
        if not header.get("success", False):
            raise RuntimeError(body.get("errorCode") or body.get("reason") or "rejected")
        return body
    finally:
        await ws.close()
