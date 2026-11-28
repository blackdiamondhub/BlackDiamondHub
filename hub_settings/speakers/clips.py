"""Signed, short-lived URLs for the hub's speech clips, and the middleware serving them.

Speakers fetch ``/speech/clip/<sha256>.<mp3|wav>?exp=<unix time>&sig=<hmac>``
from the hub over the LAN. The signature (salted HMAC of the hub's
SECRET_KEY over key, extension and expiry) is the only access check: the
middleware runs ahead of CommonMiddleware, sessions and CSRF, like
``/healthz``, because a speaker sends no cookie and whatever Host header it
was given. It only ever serves a :class:`~hub_settings.models.SpeechClip`
by key, with the clip's own content type and ``nosniff``, and supports
Range requests (Sonos and Chromecast seek with them).
"""
import re
import time

from django.http import HttpResponse
from django.utils.crypto import constant_time_compare, salted_hmac

PREFIX = "/speech/clip/"
_SALT = "hub_settings.speakers.clip"
_NAME = re.compile(r"^([0-9a-f]{64})\.(mp3|wav)$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
TTL = 600


def signature(key, ext, exp, secret=None):
    return salted_hmac(_SALT, f"{key}.{ext}:{int(exp)}", secret=secret, algorithm="sha256").hexdigest()


def path(key, ext, ttl=TTL, now=None):
    """The signed path of a clip, valid ``ttl`` seconds."""
    exp = int((now or time.time()) + ttl)
    return f"{PREFIX}{key}.{ext}?exp={exp}&sig={signature(key, ext, exp)}"


def check(key, ext, exp, sig, now=None):
    """True when ``sig`` is the hub's signature for this clip and hasn't expired."""
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < (now or time.time()):
        return False
    return constant_time_compare(str(sig or ""), signature(key, ext, exp))


def _range(header, size):
    """``(start, end)`` inclusive for a single ``bytes=`` range, ``None`` for the
    whole file, or ``False`` when it can't be satisfied."""
    m = _RANGE.match((header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None  # absent, multi-range or malformed: send it all
    if not m.group(1):
        n = int(m.group(2))
        return (max(size - n, 0), size - 1) if n and size else False
    start = int(m.group(1))
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or end < start:
        return False
    return start, end


def serve(request, key, ext):
    from hub_settings import speech

    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    if not check(key, ext, request.GET.get("exp"), request.GET.get("sig")):
        return HttpResponse(status=403)
    hit = speech.cached(key)
    if hit is None:
        return HttpResponse(status=404)
    audio, content_type = hit
    size = len(audio)
    span = _range(request.headers.get("Range"), size)
    if span is False:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        return response
    if span is None:
        body, status = audio, 200
    else:
        body, status = audio[span[0]:span[1] + 1], 206
    response = HttpResponse(b"" if request.method == "HEAD" else body, status=status, content_type=content_type)
    response["Content-Length"] = str(len(body))
    if span is not None:
        response["Content-Range"] = f"bytes {span[0]}-{span[1]}/{size}"
    response["Accept-Ranges"] = "bytes"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=600"
    return response


class ClipMiddleware:
    """Serve :data:`PREFIX` paths before ALLOWED_HOSTS, sessions and CSRF."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(PREFIX):
            return self.get_response(request)
        m = _NAME.match(request.path[len(PREFIX):])
        if not m:
            return HttpResponse(status=404)
        return serve(request, m.group(1), m.group(2))
