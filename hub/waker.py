"""Keeps apps with no homepage tile fresh by polling their status hooks.

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


def _loop(every):
    from django.db import close_old_connections

    # Let the server finish starting first.
    time.sleep(30)
    while True:
        try:
            tick()
        except Exception:
            log.exception("waking tile-less apps failed")
        finally:
            close_old_connections()
        time.sleep(every)
