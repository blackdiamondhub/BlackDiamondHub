"""Keeping container apps running while people use them.

bdh-supervisor stops ``on_demand`` apps after a quiet spell, so before the
proxy forwards a request it makes sure the app is up (:func:`ensure_running`)
and tells the supervisor the app is in use, at most once a minute
(:func:`touch`). Both are no-ops for apps the supervisor doesn't run.
"""
import asyncio
import logging
import time

from . import supervisor

log = logging.getLogger(__name__)

HOLD = 1.0           # how long a request waits for a start before the "Starting…" page
RUNNING_TTL = 60.0   # trust "it's running" for this long
ACTIVITY_EVERY = 60.0
RETRY_AFTER = 15.0   # after a failed start, don't ask again for this long

_running_until = {}
_failed_until = {}
_starting = {}
_last_activity = {}
_background = set()


def managed(install):
    return install.is_container and supervisor.enabled()


def reset():
    _running_until.clear()
    _failed_until.clear()
    _last_activity.clear()
    for task in list(_starting.values()) + list(_background):
        try:
            task.cancel()
        except RuntimeError:  # its event loop is closed
            pass
    _starting.clear()
    _background.clear()


def forget(app_id):
    """Assume ``app_id`` is stopped (its upstream refused a connection)."""
    _running_until.pop(app_id, None)


def _spawn(coro):
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _probe(app_id):
    """True when the supervisor says ``app_id`` is already running.

    The supervisor's ``start`` recreates the container even when it's up, so a
    lapsed :data:`RUNNING_TTL` must not lead straight to a start: an app polled
    less than once a minute would be killed mid-use on every poll.
    """
    try:
        reply = await supervisor.aactivity(app_id)
    except supervisor.SupervisorError as exc:
        log.debug("%s: running check failed: %s", app_id, exc)
        return False
    return isinstance(reply, dict) and reply.get("running") is True


async def _start(app_id):
    try:
        if await _probe(app_id):
            _running_until[app_id] = time.monotonic() + RUNNING_TTL
            _last_activity[app_id] = time.monotonic()
            return True
        view = await supervisor.astart(app_id, wait=True)
    except supervisor.SupervisorError as exc:
        log.warning("%s: start failed: %s", app_id, exc)
        _failed_until[app_id] = time.monotonic() + RETRY_AFTER
        return False
    finally:
        _starting.pop(app_id, None)
    if view.get("healthy") is False:
        log.warning("%s started but isn't healthy", app_id)
        _failed_until[app_id] = time.monotonic() + RETRY_AFTER
        return False
    _running_until[app_id] = time.monotonic() + RUNNING_TTL
    _last_activity[app_id] = time.monotonic()
    return True


def seen(install):
    """``install`` just called the broker, so it is up: skip the start check for a while.

    Without this the first page after a hub restart waits on the supervisor and
    shows "Starting…" even for an app that has been running all along (a
    doorbell pop-up from an ``always`` app, say).
    """
    if managed(install):
        _running_until[install.app_id] = time.monotonic() + RUNNING_TTL


async def ensure_running(install, hold=None):
    """True when the app is (or within ``hold`` seconds becomes) ready.

    False means it is still starting: the caller shows a "Starting…" page and
    the start carries on in the background.
    """
    if not managed(install):
        return True
    app_id = install.app_id
    now = time.monotonic()
    if _running_until.get(app_id, 0.0) > now or _failed_until.get(app_id, 0.0) > now:
        return True
    task = _starting.get(app_id)
    if task is None:
        task = _starting[app_id] = _spawn(_start(app_id))
    done, _pending = await asyncio.wait({task}, timeout=HOLD if hold is None else hold)
    if not done:
        return False
    # A failed start still lets the request through: the proxy reports the app as down.
    return True


async def _activity(app_id):
    try:
        reply = await supervisor.aactivity(app_id)
    except supervisor.SupervisorError as exc:
        log.debug("%s: activity not recorded: %s", app_id, exc)
        return
    if isinstance(reply, dict) and reply.get("running") is False:
        forget(app_id)


def touch(install):
    """Tell the supervisor ``install`` is in use (throttled, fire-and-forget)."""
    if not managed(install):
        return
    now = time.monotonic()
    if now - _last_activity.get(install.app_id, -ACTIVITY_EVERY) < ACTIVITY_EVERY:
        return
    _last_activity[install.app_id] = now
    _spawn(_activity(install.app_id))
