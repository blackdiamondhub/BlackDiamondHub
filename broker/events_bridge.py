"""Relays hub events to container apps and app events to the hub.

An app's ``events.subscribe`` call replaces its pattern set. Each (app,
pattern) becomes a real ``bdh_sdk.events`` subscription, so the hub's event
sources (e.g. the Home Assistant websocket) fetch what container apps want
just as they do for in-process apps. Matching events are POSTed, signed, to
the app's ``/_bdh/events`` on a small thread pool; failures are dropped.

Events an app publishes are not sent back to it.
"""
import collections
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from bdh_sdk import events as bus

log = logging.getLogger(__name__)

TIMEOUT = 3.0
MAX_PENDING = 200
ECHO_TTL = 5.0

_lock = threading.Lock()
_subs = {}  # app_id -> {pattern: Subscription}
_echo = collections.defaultdict(lambda: collections.deque(maxlen=64))  # app_id -> (topic, data, at)
_pending = 0
_pool = None


def _executor():
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bdh-broker-events")
        return _pool


def patterns(app_id):
    with _lock:
        return sorted(_subs.get(app_id, {}))


def set_patterns(app_id, wanted):
    """Make ``app_id``'s subscriptions exactly ``wanted`` (already checked)."""
    wanted = set(wanted)
    with _lock:
        current = _subs.setdefault(app_id, {})
        gone = [current.pop(p) for p in list(current) if p not in wanted]
        added = [p for p in wanted if p not in current]
        for pattern in added:
            current[pattern] = bus.subscribe(pattern, _handler(app_id, pattern))
        if not current:
            _subs.pop(app_id, None)
    for sub in gone:
        sub.cancel()


def drop(app_id):
    set_patterns(app_id, ())
    with _lock:
        _echo.pop(app_id, None)


def reset():
    for app_id in list(_subs):
        drop(app_id)


# --- Persistence ---------------------------------------------------------------
# The SDK only sends ``events.subscribe`` when an app's patterns change, so the
# hub keeps the last set in the database and puts it back after a restart.
# Every write (subscribe, restore, reconcile) for an app runs under that app's
# lock and reads the row inside it, so an older copy can't overwrite a newer one.

RECONCILE_EVERY = 60.0
_app_locks = collections.defaultdict(threading.Lock)


def _app_lock(app_id):
    with _lock:
        return _app_locks[app_id]


def subscribe(app_id, wanted):
    """An app's ``events.subscribe``: store ``wanted`` (already checked), then apply it."""
    from .models import AppSubscription

    wanted = sorted(set(wanted))
    with _app_lock(app_id):
        if wanted:
            AppSubscription.objects.update_or_create(
                app_id=app_id, kind=AppSubscription.EVENTS, instance="", defaults={"payload": wanted})
        else:
            AppSubscription.objects.filter(app_id=app_id, kind=AppSubscription.EVENTS).delete()
        set_patterns(app_id, wanted)


def allowed(install, wanted):
    """The patterns in ``wanted`` that ``install`` may still subscribe to."""
    from . import grants as g

    if install is None or not install.enabled or not isinstance(wanted, list):
        return []
    grants = install.grants
    out = []
    for pattern in wanted:
        if not isinstance(pattern, str):
            continue
        try:
            bus._segments(pattern, wildcards=True)
        except ValueError:
            continue
        if g.can_subscribe(grants, install.app_id, pattern):
            out.append(pattern)
    return sorted(set(out))


def forget(app_id):
    """The app is gone: drop its subscriptions and the stored copy."""
    from .models import AppSubscription

    with _app_lock(app_id):
        AppSubscription.objects.filter(app_id=app_id).delete()
        drop(app_id)


def sync(app_id):
    """Make ``app_id``'s live subscriptions match what's stored, minus anything
    its install may no longer subscribe to (it was disabled, or an upgrade
    dropped a permission in another process)."""
    from .models import AppInstall, AppSubscription

    with _app_lock(app_id):
        install = AppInstall.objects.filter(app_id=app_id).first()
        if install is None:
            AppSubscription.objects.filter(app_id=app_id).delete()
            drop(app_id)
            return
        row = AppSubscription.objects.filter(app_id=app_id, kind=AppSubscription.EVENTS).first()
        wanted = allowed(install, row.payload if row else [])
        if wanted != patterns(app_id):
            log.info("events for %s: %s", app_id, ", ".join(wanted) or "none")
            set_patterns(app_id, wanted)


def reconcile():
    """:func:`sync` every app that has stored or live subscriptions."""
    from .models import AppSubscription

    with _lock:
        live = set(_subs)
    stored = set(AppSubscription.objects.values_list("app_id", flat=True))
    for app_id in sorted(live | stored):
        try:
            sync(app_id)
        except Exception:
            log.exception("could not restore event subscriptions for %s", app_id)


def start(every=RECONCILE_EVERY):
    """Restore stored subscriptions now, then keep them in step with the installs."""
    thread = threading.Thread(target=_loop, args=(every,), name="bdh-broker-subs", daemon=True)
    thread.start()
    return thread


def _loop(every):
    from django.db import close_old_connections

    wait = 2.0
    while True:
        time.sleep(wait)
        try:
            reconcile()
            wait = every
        except Exception as exc:
            # Usually the database isn't up yet; try again soon.
            log.warning("could not restore event subscriptions: %s", exc)
            wait = min(every, 10.0)
        finally:
            close_old_connections()


def publish(app_id, topic, data):
    """Publish an app's event on the hub's bus, minus the echo to the app itself."""
    with _lock:
        _echo[app_id].append((topic, data, time.monotonic()))
    bus.publish(topic, data)


def _is_echo(app_id, event):
    now = time.monotonic()
    with _lock:
        marks = _echo.get(app_id)
        if not marks:
            return False
        for i, (topic, data, at) in enumerate(marks):
            if now - at > ECHO_TTL:
                continue
            if topic == event.topic and data is event.data:
                del marks[i]
                return True
    return False


def _handler(app_id, pattern):
    def forward(event):
        # An event matching several of the app's patterns goes out once.
        with _lock:
            mine = sorted(p for p in _subs.get(app_id, {}) if bus.matches(p, event.topic))
        if not mine or mine[0] != pattern:
            return
        if _is_echo(app_id, event):
            return
        _submit(app_id, event)
    forward.__qualname__ = f"broker[{app_id}]"
    return forward


def _submit(app_id, event):
    global _pending
    try:
        payload = json.loads(json.dumps({"topic": event.topic, "data": event.data, "time": event.time}))
    except (TypeError, ValueError):
        log.warning("event %s isn't JSON; not sent to %s", event.topic, app_id)
        return
    with _lock:
        if _pending >= MAX_PENDING:
            log.warning("event queue full; dropping %s for %s", event.topic, app_id)
            return
        _pending += 1
    _executor().submit(_send, app_id, payload)


def _send(app_id, payload):
    global _pending
    from django.db import close_old_connections

    from . import remote

    try:
        install = remote._install(app_id)
        # Events don't wake a stopped on-demand app.
        resp = remote.signed_request(install, "POST", "/_bdh/events", payload, timeout=TIMEOUT, wake=False)
        if resp.status_code != 200:
            log.info("%s refused event %s: HTTP %s", app_id, payload["topic"], resp.status_code)
    except Exception as exc:
        log.info("event %s to %s dropped: %s", payload["topic"], app_id, exc)
    finally:
        close_old_connections()
        with _lock:
            _pending -= 1
