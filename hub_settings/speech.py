"""The hub's own text-to-speech, installed as ``bdh_sdk.speakers.set_synth``.

Announcements on Sonos and spoken alerts on the screens use the audio made
here instead of Home Assistant's TTS, so they don't depend on Nabu Casa.

Engines, tried in order until one works (each failure is logged):

1. Azure Speech, when Settings → Speech picks it and an Azure key is saved.
2. Google Translate's free voice (no account), the same one gTTS and HA's
   ``google_translate`` integration use.
3. When both fail, :func:`synth` raises and bdh-sdk falls back to HA's TTS
   engine. With the engine set to "Home Assistant", :func:`synth` returns
   ``None`` and HA speaks every time (the 0.48 behaviour).

Voice: the voice saved on Settings → Speech is the house voice and always
wins. Until one is saved, an app's ``voice`` is used for Azure when it is an
Azure voice name, either in full (``en-US-AvaNeural``) or short
(``AvaNeural``, taken as en-US); otherwise :data:`DEFAULT_VOICE`. Google has
one voice and ignores it.

Every clip is kept in :class:`~hub_settings.models.SpeechClip`, keyed by the
sha256 of (engine, voice, text with whitespace collapsed), so a message is
only made once per engine and voice; a small in-memory layer saves the
database trip for the most recent ones. ``manage.py speech_prune`` (daily,
from the container entrypoint) deletes clips unused for :data:`PRUNE_DAYS`.
"""
import hashlib
import io
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import timedelta
from xml.sax.saxutils import escape, quoteattr

import requests
from django.db import DatabaseError
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

log = logging.getLogger(__name__)

VAULT_OWNER = "hub"
AZURE_KEY = "speech_azure_key"
ENGINE_KEY, REGION_KEY, VOICE_KEY = "speech_engine", "speech_azure_region", "speech_voice"

GOOGLE, AZURE, HA = "google", "azure", "ha"
ENGINES = {
    GOOGLE: "Google (free, no account)",
    AZURE: "Azure Speech",
    HA: "Home Assistant's TTS",
}
DEFAULT_ENGINE = GOOGLE
DEFAULT_REGION = "westus3"
DEFAULT_VOICE = "en-US-AvaNeural"
REGION_RE = re.compile(r"^[a-z0-9]{2,30}$")
VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9:]+Neural$")
SHORT_VOICE_RE = re.compile(r"^[A-Za-z0-9]+Neural$")

AZURE_FORMAT = "audio-24khz-96kbitrate-mono-mp3"
MP3 = "audio/mpeg"
TIMEOUT = 10
PRUNE_DAYS = 90
TOUCH_EVERY = timedelta(days=1)
_MEM_SIZE = 64
_VOICES_TTL = 24 * 3600


class SpeechError(Exception):
    """No engine could make the speech. Never holds the Azure key."""


def max_text():
    from bdh_sdk.speakers import MAX_TTS_TEXT

    return MAX_TTS_TEXT


def normalize(text):
    return " ".join((text or "").split())


def clip_key(engine, voice, text):
    return hashlib.sha256(f"{engine}\0{voice}\0{text}".encode("utf-8")).hexdigest()


# --- settings -------------------------------------------------------------------

def config():
    """``{engine, region, voice}``; ``voice`` is ``None`` until an admin picks one."""
    from bdh_sdk.settings import app_setting

    engine = app_setting("hub", ENGINE_KEY, DEFAULT_ENGINE)
    region = app_setting("hub", REGION_KEY, DEFAULT_REGION) or DEFAULT_REGION
    voice = app_setting("hub", VOICE_KEY, None) or None
    return {
        "engine": engine if engine in ENGINES else DEFAULT_ENGINE,
        "region": region if REGION_RE.match(str(region)) else DEFAULT_REGION,
        "voice": voice if voice and VOICE_RE.match(str(voice)) else None,
    }


@sensitive_variables()
def azure_key():
    from . import vault

    try:
        return vault.get_secret(VAULT_OWNER, AZURE_KEY)
    except (KeyError, vault.SecretStoreError):
        return ""


def key_saved():
    from . import vault

    return AZURE_KEY in vault.names(VAULT_OWNER)


def azure_voice(requested, house):
    """The Azure voice to use: the house voice, else the app's, else the default."""
    if house:
        return house
    name = (requested or "").strip()
    if SHORT_VOICE_RE.match(name):
        name = f"{DEFAULT_VOICE.rsplit('-', 1)[0]}-{name}"
    return name if VOICE_RE.match(name) else DEFAULT_VOICE


# --- engines ----------------------------------------------------------------------

def google(text, voice=None, cfg=None):
    from gtts import gTTS

    out = io.BytesIO()
    gTTS(text, lang="en", tld="com", lang_check=False, timeout=TIMEOUT).write_to_fp(out)
    audio = out.getvalue()
    if not audio:
        raise SpeechError("Google returned no audio")
    return audio, MP3


def _azure_url(region, path):
    return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/{path}"


def ssml(text, voice):
    lang = "-".join(voice.split("-")[:2])
    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang={quoteattr(lang)}>"
            f"<voice name={quoteattr(voice)}>{escape(text)}</voice></speak>")


@sensitive_variables("key", "headers")
def azure(text, voice, cfg=None, key=None):
    cfg = cfg or config()
    key = key or azure_key()
    if not key:
        raise SpeechError("no Azure Speech key saved")
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": AZURE_FORMAT,
        "User-Agent": "BlackDiamondHub",
    }
    try:
        resp = requests.post(_azure_url(cfg["region"], "v1"), data=ssml(text, voice).encode("utf-8"),
                             headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SpeechError(f"can't reach Azure Speech ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise SpeechError(f"Azure Speech answered HTTP {resp.status_code}")
    if not resp.content:
        raise SpeechError("Azure Speech returned no audio")
    return resp.content, MP3


_voices = {}  # region -> (monotonic time, [(short name, label)])
_voices_lock = threading.Lock()


@sensitive_variables("key")
def voices(region=None, key=None):
    """English neural Azure voices as ``(name, label)``, cached a day; ``[]`` when unknown."""
    region = region or config()["region"]
    with _voices_lock:
        hit = _voices.get(region)
        if hit and time.monotonic() - hit[0] < _VOICES_TTL:
            return hit[1]
    key = key or azure_key()
    if not key:
        return []
    try:
        resp = requests.get(_azure_url(region, "voices/list"), headers={"Ocp-Apim-Subscription-Key": key},
                            timeout=TIMEOUT)
        rows = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        rows = None
    if not isinstance(rows, list):
        log.warning("speech: couldn't list Azure voices for %s", region)
        return []
    out = []
    for v in rows:
        if not isinstance(v, dict):
            continue
        name = str(v.get("ShortName") or "")
        if not name.startswith("en-") or v.get("VoiceType") != "Neural" or not VOICE_RE.match(name):
            continue
        label = f"{v.get('DisplayName') or name} ({v.get('Locale') or ''}, {v.get('Gender') or ''})"
        out.append((name, label))
    out.sort(key=lambda p: (not p[0].startswith("en-US-"), p[1].lower()))
    with _voices_lock:
        _voices[region] = (time.monotonic(), out)
    return out


def clear_voices():
    with _voices_lock:
        _voices.clear()


_ENGINE_FUNCS = {GOOGLE: google, AZURE: azure}


# --- cache ------------------------------------------------------------------------

_mem = OrderedDict()  # key -> (audio, content type, last touched)
_mem_lock = threading.Lock()


def clear_memory():
    with _mem_lock:
        _mem.clear()


def _touch(key, now):
    from .models import SpeechClip

    try:
        SpeechClip.objects.filter(key=key).update(last_used=now)
    except DatabaseError:
        log.warning("speech: couldn't update clip %s", key[:12], exc_info=True)


def cached(key):
    """``(audio, content type)`` of a saved clip, or ``None``."""
    now = timezone.now()
    with _mem_lock:
        hit = _mem.get(key)
        if hit:
            _mem.move_to_end(key)
            if now - hit[2] > TOUCH_EVERY:
                _mem[key] = (hit[0], hit[1], now)
    if hit:
        if now - hit[2] > TOUCH_EVERY:
            _touch(key, now)
        return hit[0], hit[1]
    from .models import SpeechClip

    try:
        row = SpeechClip.objects.filter(key=key).first()
    except DatabaseError:
        log.warning("speech: clip cache unavailable", exc_info=True)
        return None
    if row is None:
        return None
    if now - row.last_used > TOUCH_EVERY:
        _touch(key, now)
    audio = bytes(row.audio)
    if row.kind != SpeechClip.CUSTOM:  # apps' clips are large and played once
        _remember(key, audio, row.content_type, now)
    return audio, row.content_type


def _remember(key, audio, content_type, now):
    with _mem_lock:
        _mem[key] = (audio, content_type, now)
        _mem.move_to_end(key)
        while len(_mem) > _MEM_SIZE:
            _mem.popitem(last=False)


def save(key, engine, voice, text, audio, content_type):
    from .models import SpeechClip

    now = timezone.now()
    try:
        SpeechClip.objects.update_or_create(key=key, defaults={
            "engine": engine, "voice": voice, "text": text, "audio": audio,
            "content_type": content_type, "last_used": now,
        })
    except DatabaseError:
        log.warning("speech: couldn't save clip %s", key[:12], exc_info=True)
    _remember(key, audio, content_type, now)


def prune(days=PRUNE_DAYS):
    """Delete clips nobody has used for ``days``; returns how many."""
    from .models import SpeechClip

    clear_memory()
    return SpeechClip.objects.filter(last_used__lt=timezone.now() - timedelta(days=days)).delete()[0]


# --- the synth ----------------------------------------------------------------------

def chain(voice=None, cfg=None):
    """``[(engine, voice)]`` to try, best first; empty when HA does the speaking."""
    cfg = cfg or config()
    if cfg["engine"] == HA:
        return []
    out = []
    if cfg["engine"] == AZURE:
        if key_saved():
            out.append((AZURE, azure_voice(voice, cfg["voice"])))
        else:
            log.warning("speech: Azure Speech picked but no key saved, using Google")
    out.append((GOOGLE, ""))
    return out


def speak(message, voice=None):
    """``(audio, content type, engine)`` for ``message``, or ``None`` when HA speaks.

    Raises ValueError for empty or over-long text, SpeechError when every
    engine failed.
    """
    text = normalize(message)
    if not text:
        raise ValueError("message is required")
    if len(text) > max_text():
        raise ValueError(f"message must be at most {max_text()} characters")
    cfg = config()
    steps = chain(voice, cfg)
    if not steps:
        return None
    for i, (engine, engine_voice) in enumerate(steps):
        key = clip_key(engine, engine_voice, text)
        hit = cached(key)
        if hit:
            return hit[0], hit[1], engine
        try:
            audio, content_type = _ENGINE_FUNCS[engine](text, engine_voice, cfg)
        except Exception as exc:
            nxt = steps[i + 1][0] if i + 1 < len(steps) else "Home Assistant"
            log.warning("speech: %s failed (%s), falling back to %s", engine, exc, nxt)
            continue
        save(key, engine, engine_voice, text, audio, content_type)
        return audio, content_type, engine
    raise SpeechError("every speech engine failed")


def synth(message, voice=None):
    """``bdh_sdk.speakers`` synth: ``(audio, content type)``, ``None`` for HA, or raises."""
    out = speak(message, voice)
    return None if out is None else out[:2]
