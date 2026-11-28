"""Sonos, directly: soco for the room list, the local websocket API for clips.

Rooms come from the household topology (``ZoneGroupState``), asked of any
speaker the hub already knows, so satellites, subs and the second half of a
stereo pair never show up. Only when no known speaker answers does the hub
run SSDP discovery, bound to its LAN address.

Clips use ``audioClip:1 loadAudioClip`` over the speaker's websocket (what
Home Assistant's Sonos announce uses): the speaker ducks what it's playing,
plays the clip and resumes by itself, so nothing is snapshotted or
restored. The command is sent once; ``sonos-websocket``'s own retry would
play a slow speaker's clip twice.
"""
import asyncio
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


def _rooms(zones):
    return [{"uid": z.uid, "name": z.player_name or z.uid, "address": z.ip_address} for z in zones]


def topology(seeds=()):
    """``[{uid, name, address}]`` of every visible room, or None when no Sonos answered.

    Asks the ``seeds`` (last-known addresses) first, then SSDP discovery.
    Blocking.
    """
    import soco

    for address in dict.fromkeys(a for a in seeds if a):
        try:
            return _rooms(soco.SoCo(address).visible_zones)
        except Exception as exc:  # noqa: BLE001 - moved, off or not a Sonos any more
            log.debug("speakers: Sonos %s didn't answer: %s", address, exc)
    try:
        found = soco.discover(timeout=DISCOVER_TIMEOUT, interface_addr=lan_address(), allow_network_scan=False)
    except Exception as exc:  # noqa: BLE001 - no LAN, multicast blocked
        log.warning("speakers: Sonos discovery failed: %s", exc)
        return None
    if not found:
        return None
    for zone in found:
        try:
            return _rooms(zone.visible_zones)
        except Exception as exc:  # noqa: BLE001
            log.debug("speakers: Sonos %s didn't answer: %s", zone.ip_address, exc)
    return None


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
