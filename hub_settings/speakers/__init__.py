"""The hub's own speaker layer, installed as ``bdh_sdk.speakers.set_backend(HubSpeakers())``.

Every announcement made in the hub (the broker's ``speakers.*`` ops for
container apps, Settings → Speech, alerts) goes through here:

- Sonos plays directly through its local websocket API (:mod:`.sonos`): the
  chime and the speech as ONE MP3 (:mod:`.audio`) the speaker fetches from
  the hub (:mod:`.clips`), ducking and resuming whatever is playing.
- Alexa speaks the text itself (``notify.alexa_media``, no chime).
- Other Home Assistant media players that can announce, and Sonos rooms the
  hub can't reach directly, get the same clip URL through HA's
  ``media_player.play_media`` (announce). That fallback is used only when a
  Sonos connection fails before anything was sent: a clip sent but not
  confirmed counts as failed, so nothing plays twice.

Announcements take turns: the next starts when the last one's clip has
finished (:class:`Gate`); more than :data:`MAX_WAITING` waiting are refused.
Each call has one deadline (:data:`CALL_TIMEOUT` from entry, under the SDK's
120 s): finding speakers, making the speech and waiting for a turn all spend
from it, and a call without time left to send is refused as busy.
Speakers are known by native ids and by the HA entity ids apps saved before
(see :mod:`.inventory`).
"""
import asyncio
import hashlib
import logging
import os
import time
from importlib import resources

from django.db import DatabaseError
from django.utils import timezone

from . import audio, clips, inventory, loop, sonos

log = logging.getLogger(__name__)

MAX_WAITING = 3
CALL_TIMEOUT = 110  # under the SDK's 120 s announce timeout, from the call's start
WAIT_BUDGET = 80
SEND_MARGIN = 15  # left for sending once it's our turn (Sonos connect + reply, HA call)
MAX_HOLD = 65
HOLD_MARGIN = 0.5
GAP = 0.3  # between a chime and speech played as two clips
CUSTOM_TTL = 3600
MAX_CUSTOM = 20  # app clips kept at most (the newest)
DEFAULT_MAX_VOLUME = 60
MAX_VOLUME_KEY = "speakers_max_volume"
LAN_BASE_KEY = "speech_lan_base"


class Busy(Exception):
    pass


def _sdk():
    from bdh_sdk import speakers

    return speakers


# --- settings ------------------------------------------------------------------------

def max_volume():
    from bdh_sdk.settings import app_setting

    try:
        value = int(app_setting("hub", MAX_VOLUME_KEY, DEFAULT_MAX_VOLUME))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_VOLUME
    return max(0, min(100, value))


def lan_base_override():
    from bdh_sdk.settings import app_setting

    return (app_setting("hub", LAN_BASE_KEY, "") or "").strip().rstrip("/")


def base_url(peer):
    """``http://<hub address toward peer>:<port>``, or the Settings override."""
    override = lan_base_override()
    if override:
        return override
    return f"http://{sonos.lan_address(peer)}:{int(os.getenv('PORT', '8000'))}"


def night_now():
    from display.schedule import is_night, lodge_minute, window

    night = window()
    return bool(night) and is_night(lodge_minute(), night["from"], night["until"])


# --- clips ---------------------------------------------------------------------------

class Clip:
    """One stored clip: the key it's served under, its extension and length."""

    def __init__(self, key, ext, seconds):
        self.key, self.ext, self.seconds = key, ext, seconds

    def url(self, base):
        ttl = max(clips.TTL, int(self.seconds) + 300)
        return base + clips.path(self.key, self.ext, ttl=ttl)


def _store(key, kind, data, ext, text=""):
    """Save a clip; announcement clips are also kept in the speech memory cache, app clips aren't."""
    from hub_settings import speech
    from hub_settings.models import SpeechClip

    content_type = audio.CONTENT_TYPES[ext]
    now = timezone.now()
    try:
        SpeechClip.objects.update_or_create(key=key, defaults={
            "kind": kind, "engine": kind, "voice": "", "text": text[:1000], "audio": data,
            "content_type": content_type, "last_used": now})
    except DatabaseError:
        log.warning("speakers: couldn't save clip %s", key[:12], exc_info=True)
    if kind != SpeechClip.CUSTOM:
        speech._remember(key, data, content_type, now)


def _sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
        h.update(b"\0")
    return h.hexdigest()


def chime_bytes(chime):
    return resources.files(__package__).joinpath("chimes", f"{chime}.mp3").read_bytes()


def announce_clips(message, chime, speech_audio):
    """The clips to play for an announcement: one MP3 when chime and speech join, else two."""
    from hub_settings.models import SpeechClip

    kind = audio.sniff(speech_audio) or audio.MP3
    try:
        speech = audio.parse_mp3(speech_audio) if kind == audio.MP3 else None
        speech_seconds = speech.duration if speech else audio.duration(speech_audio, kind)
    except audio.AudioError:
        speech, speech_seconds = None, 0.0
    speech_sha = hashlib.sha256(speech_audio).hexdigest()
    if chime is None:
        key = _sha("speech", speech_sha)
        _store(key, SpeechClip.ANNOUNCE, speech_audio, kind, message)
        return [Clip(key, kind, speech_seconds)]
    chime_mp3 = audio.parse_mp3(chime_bytes(chime))
    joined = audio.join(chime_mp3, speech) if speech is not None else None
    if joined is not None:
        key = _sha("announce", chime, speech_sha)
        _store(key, SpeechClip.ANNOUNCE, joined.frames, audio.MP3, message)
        return [Clip(key, audio.MP3, joined.duration)]
    log.info("speakers: speech isn't %d Hz mono MP3, playing the chime separately", chime_mp3.rate)
    chime_key, speech_key = _sha("chime", chime), _sha("speech", speech_sha)
    _store(chime_key, SpeechClip.ANNOUNCE, chime_mp3.frames, audio.MP3, f"chime {chime}")
    _store(speech_key, SpeechClip.ANNOUNCE, speech_audio, kind, message)
    return [Clip(chime_key, audio.MP3, chime_mp3.duration), Clip(speech_key, kind, speech_seconds)]


def prune_custom(max_age=CUSTOM_TTL, keep=None):
    """Delete app clips older than ``max_age`` seconds and all but the newest ``keep``; returns how many."""
    from datetime import timedelta

    from hub_settings.models import SpeechClip

    keep = MAX_CUSTOM if keep is None else keep
    cutoff = timezone.now() - timedelta(seconds=max_age)
    try:
        custom = SpeechClip.objects.filter(kind=SpeechClip.CUSTOM)
        deleted = custom.filter(last_used__lt=cutoff).delete()[0]
        extra = list(custom.order_by("-last_used", "-pk").values_list("pk", flat=True)[keep:])
        if extra:
            deleted += SpeechClip.objects.filter(pk__in=extra).delete()[0]
        return deleted
    except DatabaseError:
        log.warning("speakers: couldn't prune app clips", exc_info=True)
        return 0


# --- routing ---------------------------------------------------------------------------

class Job:
    """What to do for one speaker: ``route`` is sonos, ha_media or alexa."""

    def __init__(self, ident, row, route):
        self.id, self.row, self.route = ident, row, route
        self.path = route


def plan(resolved, text):
    """``(jobs, statuses)``: a job per playable id, a final status for the rest."""
    sdk = _sdk()
    jobs, statuses = [], {}
    for ident, row in resolved.items():
        if row is None:
            statuses[ident] = sdk.FAILED
        elif row.kind == "sonos":
            if row.caps.get("audio_clip") and row.address:
                jobs.append(Job(ident, row, "sonos"))
            elif row.ha_entity and row.ha_instance:
                if row.online:
                    jobs.append(Job(ident, row, "ha_media"))
                else:
                    statuses[ident] = sdk.OFFLINE
            elif row.address:
                statuses[ident] = sdk.UNSUPPORTED
            else:
                statuses[ident] = sdk.OFFLINE
        elif row.kind == "alexa":
            if not text:
                statuses[ident] = sdk.UNSUPPORTED
            elif not row.online:
                statuses[ident] = sdk.OFFLINE
            else:
                jobs.append(Job(ident, row, "alexa"))
        else:
            if not row.online:
                statuses[ident] = sdk.OFFLINE
            else:
                jobs.append(Job(ident, row, "ha_media"))
    return jobs, statuses


def _ha_host(instance):
    from urllib.parse import urlparse

    from hub_settings import ha as hub_ha

    try:
        return urlparse(hub_ha.connection(instance)[0]).hostname
    except Exception:  # noqa: BLE001
        return None


def urls_for(jobs, clip_list):
    """Each audio job's clip URLs, built toward the speaker (or its HA host)."""
    bases = {}
    for job in jobs:
        if job.route == "alexa" or not clip_list:
            job.urls = []
            continue
        peer = job.row.address or _ha_host(job.row.ha_instance)
        if peer not in bases:
            bases[peer] = base_url(peer)
        job.urls = [(c.url(bases[peer]), c.seconds) for c in clip_list]


# --- sending (on the loop) ---------------------------------------------------------------

class Gate:
    """One announcement at a time, held until its clip has played."""

    def __init__(self):
        self.lock = None
        self.waiting = 0

    async def acquire(self, timeout):
        if self.lock is None:
            self.lock = asyncio.Lock()
        if self.lock.locked() and self.waiting >= MAX_WAITING:
            raise Busy("too many announcements waiting")
        self.waiting += 1
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=timeout)
        except TimeoutError:
            raise Busy("timed out waiting for the speakers") from None
        finally:
            self.waiting -= 1

    def hold(self, seconds):
        """Release after ``seconds`` (now when nothing played)."""
        if seconds <= 0:
            self.lock.release()
        else:
            asyncio.get_running_loop().call_later(min(seconds, MAX_HOLD), self.lock.release)


gate = Gate()


def _call_service(*args, **kwargs):
    from bdh_sdk import ha

    return ha.call_service(*args, **kwargs)


def _media_extra(url, volume):
    extra = {"media_content_id": url, "media_content_type": "music", "announce": True}
    if volume is not None:
        extra["extra"] = {"volume": int(volume)}
    return extra


async def _ha_play(row, urls, volume):
    for i, (url, seconds) in enumerate(urls):
        ok, err = await loop.blocking(lambda u=url: _call_service(
            "media_player", "play_media", [row.ha_entity], _media_extra(u, volume), instance=row.ha_instance))
        if not ok:
            raise RuntimeError(err or "Home Assistant refused")
        if i + 1 < len(urls):
            await asyncio.sleep(seconds + GAP)


async def _sonos_play(job, volume):
    sdk = _sdk()
    row = job.row
    i = 0
    try:
        for i, (url, seconds) in enumerate(job.urls):
            await sonos.play_clip(row.native_id.split(":", 1)[1], row.address, url, volume)
            if i + 1 < len(job.urls):
                await asyncio.sleep(seconds + GAP)
        job.online = True
        return sdk.PLAYED
    except sonos.NotSent as exc:
        job.online = False
        if i > 0:
            log.warning("speakers: %s dropped mid-announcement: %s", row.name, exc)
            return sdk.FAILED
        if not (row.ha_entity and row.ha_instance):
            log.info("speakers: %s unreachable: %s", row.name, exc)
            return sdk.OFFLINE
        job.path = "sonos>ha"
        log.info("speakers: %s unreachable (%s), playing through Home Assistant", row.name, exc)
        try:
            await _ha_play(row, job.urls, volume)
            return sdk.PLAYED
        except Exception as exc2:  # noqa: BLE001
            log.warning("speakers: %s through Home Assistant failed: %s", row.name, exc2)
            return sdk.FAILED
    except Exception as exc:  # noqa: BLE001 - sent, not confirmed: don't risk playing it twice
        job.online = True
        log.warning("speakers: %s didn't confirm the clip: %s", row.name, exc)
        return sdk.FAILED


async def _alexa(jobs, message):
    sdk = _sdk()
    by_instance = {}
    for job in jobs:
        by_instance.setdefault(job.row.ha_instance, []).append(job)
    out = {}
    for instance, group in by_instance.items():
        targets = [j.row.ha_entity for j in group]
        try:
            ok, err = await loop.blocking(lambda i=instance, t=targets: _call_service(
                "notify", "alexa_media", None,
                {"message": message, "target": t, "data": {"type": "announce"}}, instance=i))
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
        if not ok:
            log.warning("speakers: Alexa announce failed: %s", err)
        for job in group:
            out[job.id] = sdk.PLAYED if ok else sdk.FAILED
    return out


async def _media(job, volume):
    sdk = _sdk()
    try:
        await _ha_play(job.row, job.urls, volume)
        return sdk.PLAYED
    except Exception as exc:  # noqa: BLE001
        log.warning("speakers: %s through Home Assistant failed: %s", job.row.name, exc)
        return sdk.FAILED


async def deliver(jobs, message, volume, seconds):
    """Send every job in parallel (the turn is already ours, see :func:`_turn`); ``{id: status}``."""
    sdk = _sdk()
    played = False
    try:
        tasks, ids = [], []
        alexa = [j for j in jobs if j.route == "alexa"]
        for job in jobs:
            if job.route == "sonos":
                tasks.append(_sonos_play(job, volume))
                ids.append(job.id)
            elif job.route == "ha_media":
                tasks.append(_media(job, volume))
                ids.append(job.id)
        results = dict(zip(ids, await asyncio.gather(*tasks))) if tasks else {}
        if alexa:
            results.update(await _alexa(alexa, message))
        played = any(results.get(j.id) == sdk.PLAYED and j.route != "alexa" for j in jobs)
        return results
    finally:
        gate.hold(seconds + HOLD_MARGIN if played else 0)


def _remaining(deadline):
    return deadline - time.monotonic()


def _run(coro, deadline):
    sdk = _sdk()
    try:
        return loop.run(coro, max(0.1, _remaining(deadline)))
    except Busy as exc:
        raise sdk.AnnounceError(f"speakers busy: {exc}") from None
    except TimeoutError:
        raise sdk.AnnounceError("timed out") from None


def _turn(deadline):
    """Wait for our turn on the speakers, leaving :data:`SEND_MARGIN` before ``deadline``."""
    wait = min(WAIT_BUDGET, _remaining(deadline) - SEND_MARGIN)
    if wait <= 0:
        raise _sdk().AnnounceError("speakers busy: no time left to play")
    _run(gate.acquire(wait), deadline)


def _release():
    """Give the turn back without playing (on the loop: the gate lives there)."""
    loop.loop().call_soon_threadsafe(gate.hold, 0)


def _send(jobs, message, volume, seconds, deadline, prepare=None):
    """Take a turn, run ``prepare()`` (database work, here in the caller's thread), send."""
    _turn(deadline)
    try:
        if prepare is not None:
            prepare()
        coro = deliver(jobs, message, volume, seconds)
    except BaseException:
        _release()
        raise
    return _run(coro, deadline)


def _finish(what, ids, statuses, jobs, detail):
    sdk = _sdk()
    paths = {j.id: j.path for j in jobs}
    for job in jobs:
        if hasattr(job, "online") and job.row.pk:
            inventory.mark(job.row, job.online)
    log.info("speakers: %s: %s", what, ", ".join(
        f"{i}={paths.get(i, '-')}:{statuses.get(i)}" for i in ids) or "no speakers")
    if detail:
        return {i: statuses[i] for i in ids}
    failed = [i for i in ids if statuses[i] != sdk.PLAYED]
    if failed and len(failed) == len(ids):
        raise sdk.AnnounceError("announcement failed on every speaker")
    return failed


def _ids(speakers):
    sdk = _sdk()
    if isinstance(speakers, str) or not all(isinstance(s, str) for s in speakers):
        raise ValueError("speakers must be a list of ids")
    ids = list(dict.fromkeys(speakers))
    if not ids:
        raise sdk.AnnounceError("no speakers picked")
    return ids


def _volume(volume):
    if volume is None:
        return None
    if isinstance(volume, bool) or not 0 <= int(volume) <= 100:
        raise ValueError("volume must be 0-100")
    return int(volume)


class HubSpeakers:
    """``bdh_sdk.speakers`` backend (see the module docstring)."""

    def list_speakers(self):
        return inventory.pairs()

    def devices(self):
        return inventory.devices()

    def tts_audio(self, message, tts_engine=None, voice=None):
        sdk = _sdk()
        return sdk.ha_tts_audio(message, tts_engine or sdk.DEFAULT_TTS, voice)

    def announce(self, message, speakers, volume=None, chime=None, tts_engine=None, voice=None, detail=False):
        from bdh_sdk.alerts import CHIMES, SILENT

        deadline = time.monotonic() + CALL_TIMEOUT
        sdk = _sdk()
        # The one message limit (SDK MAX_TTS_TEXT, 200), before anything plays anywhere.
        message = sdk.check_message(message)
        if chime not in (None, SILENT) and chime not in CHIMES:
            raise ValueError(f"unknown chime {chime!r}")
        volume = _volume(volume)
        ids = _ids(speakers)
        message = " ".join(message.split())
        jobs, statuses = plan(inventory.resolve(ids, wait=_remaining(deadline) - SEND_MARGIN), message)
        clip_list, seconds = [], 0.0
        if any(j.route != "alexa" for j in jobs):
            try:
                speech = sdk.ha_tts_audio(message, tts_engine or sdk.DEFAULT_TTS, voice)[0]
                clip_list = announce_clips(message, None if chime in (None, SILENT) else chime, speech)
                seconds = sum(c.seconds for c in clip_list) + GAP * (len(clip_list) - 1)
            except Exception as exc:  # noqa: BLE001 - no engine, HA down or unreadable audio
                log.warning("speakers: couldn't make the speech: %s", exc)
                for job in [j for j in jobs if j.route != "alexa"]:
                    statuses[job.id] = sdk.FAILED
                jobs = [j for j in jobs if j.route == "alexa"]
        urls_for(jobs, clip_list)
        if jobs:
            statuses.update(_send(jobs, message, volume, seconds, deadline))
        return _finish(f"announce {message[:40]!r} chime={chime or '-'}", ids, statuses, jobs, detail)

    def play(self, data, speakers, volume=None, night=False, detail=False):
        from hub_settings.models import SpeechClip

        deadline = time.monotonic() + CALL_TIMEOUT
        sdk = _sdk()
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("audio is required")
        if len(data) > sdk.MAX_PLAY_BYTES:
            raise ValueError("audio must be at most 2 MB")
        kind = audio.sniff(data)
        if kind is None:
            raise ValueError("audio must be an MP3 or WAV")
        try:
            seconds = audio.duration(data, kind)
        except audio.AudioError as exc:
            raise ValueError(f"unreadable audio: {exc}") from None
        if seconds > sdk.MAX_PLAY_SECONDS:
            raise ValueError(f"audio must be at most {sdk.MAX_PLAY_SECONDS} seconds")
        volume = _volume(volume)
        if volume is not None:
            volume = min(volume, max_volume())
        ids = _ids(speakers)
        if not night and night_now():
            raise sdk.AnnounceError("it's night on the hub (Settings → Display); pass night=True to play anyway")
        jobs, statuses = plan(inventory.resolve(ids, wait=_remaining(deadline) - SEND_MARGIN), None)
        if jobs:
            data = bytes(data)
            key = _sha("custom", hashlib.sha256(data).hexdigest())
            urls_for(jobs, [Clip(key, kind, seconds)])

            def prepare():  # stored only once it's about to play
                _store(key, SpeechClip.CUSTOM, data, kind)
                prune_custom()

            statuses.update(_send(jobs, None, volume, seconds, deadline, prepare))
        return _finish(f"play {seconds:.1f}s clip", ids, statuses, jobs, detail)
