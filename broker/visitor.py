"""Who a visitor of an app's port is, as the hub tells the app."""
import threading
import time
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user
from django.contrib.auth.models import AnonymousUser

from .remote import hub_origin

CACHE_TTL = 5.0
_cache = {}
_lock = threading.Lock()


def identity(user, mode, stay, host):
    """The ``X-BDH-Identity`` dict for ``user`` seen through ``host``."""
    ident = {"mode": mode, "stay": stay or "", "o": hub_origin(host)}
    if user is not None and user.is_authenticated:
        ident.update(u=user.get_username(), n=user.first_name or "", staff=bool(user.is_staff))
    return ident


def for_request(request):
    """Identity for a Django request on the hub (``request.user`` set)."""
    from hub.context_processors import stay_key
    from hub.modes import resolve_mode

    return identity(request.user, resolve_mode(request), stay_key(request)["hub_stay_key"](),
                    request.get_host())


def user_for_session(session_key):
    """The hub user signed in with ``session_key`` (the hub's session cookie)."""
    if not session_key:
        return AnonymousUser()
    from importlib import import_module

    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    try:
        return get_user(SimpleNamespace(session=store))
    except Exception:
        return AnonymousUser()


def for_session(session_key, host):
    """Identity for a visitor with the hub session cookie ``session_key`` (sync;
    cached a few seconds, since every asset an app page loads asks)."""
    from hub.context_processors import stay_key
    from hub.modes import resolve_mode

    key = (session_key or "", host or "")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return dict(hit[1])
    user = user_for_session(session_key)
    req = SimpleNamespace(user=user)
    ident = identity(user, resolve_mode(req), stay_key(req)["hub_stay_key"](), host)
    with _lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (now, ident)
    return dict(ident)


def clear():
    with _lock:
        _cache.clear()
