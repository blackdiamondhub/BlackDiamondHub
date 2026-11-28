"""Keeps apps with no homepage tile fresh by polling their status hooks,
and keeps the assistant's tools warm (``hub.tools.warm``).

The homepage wakes sleeping apps by polling their tiles; an app without a
tile (a background feed) would otherwise sleep until someone opens it.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)

EVERY = 600
# How often to check whether an assistant app was installed, updated or removed.
CHECK = 30


def start(every=EVERY, check=CHECK):
    thread = threading.Thread(target=_loop, args=(every, check), name="hub-waker", daemon=True)
    thread.start()
    return thread


def tick():
    from . import status

    return status.wake_hidden()


def warm_tools():
    from . import tools

    return tools.warm()


def rewarm_tools():
    from . import tools

    return tools.warm_if_changed()


def _run(jobs):
    from django.db import close_old_connections

    for job, what in jobs:
        try:
            job()
        except Exception:
            log.exception("%s failed", what)
        finally:
            close_old_connections()


def _loop(every, check=CHECK):
    # Let the server finish starting first.
    time.sleep(30)
    while True:
        _run(((tick, "waking tile-less apps"), (warm_tools, "warming assistant tools")))
        due = time.monotonic() + every
        while time.monotonic() < due:
            time.sleep(min(check, max(0, due - time.monotonic())))
            _run(((rewarm_tools, "re-warming assistant tools"),))
