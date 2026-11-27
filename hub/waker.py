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


def start(every=EVERY):
    thread = threading.Thread(target=_loop, args=(every,), name="hub-waker", daemon=True)
    thread.start()
    return thread


def tick():
    from . import status

    return status.wake_hidden()


def warm_tools():
    from . import tools

    return tools.warm()


def _loop(every):
    from django.db import close_old_connections

    # Let the server finish starting first.
    time.sleep(30)
    while True:
        for job, what in ((tick, "waking tile-less apps"), (warm_tools, "warming assistant tools")):
            try:
                job()
            except Exception:
                log.exception("%s failed", what)
            finally:
                close_old_connections()
        time.sleep(every)
