"""Background work for the Apps pages: one job per app at a time.

An install or update can take minutes (the supervisor downloads and installs
the wheel), longer than a request should wait, so the store starts it here and
the page polls :func:`get`. State lives in this process only; a page that finds
an app half-installed with no job (the hub restarted) starts a new one.
"""
import logging
import threading
import time

from django.db import connections

log = logging.getLogger(__name__)

RUNNING, DONE, FAILED = "running", "done", "failed"
KEEP = 600  # seconds a finished job stays visible

# Tests set this to run jobs synchronously.
inline = False

_lock = threading.Lock()
_jobs = {}


def get(app_id):
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] != RUNNING and time.monotonic() - job["ended"] > KEEP:
            del _jobs[app_id]
            job = None
        return dict(job) if job else None


def running(app_id):
    job = get(app_id)
    return bool(job and job["state"] == RUNNING)


def clear(app_id=None):
    with _lock:
        if app_id is None:
            _jobs.clear()
        else:
            _jobs.pop(app_id, None)


def start(app_id, label, fn, *args, info=None, **kwargs):
    """Run ``fn(*args, **kwargs)`` for ``app_id``; False if a job is already running.

    ``info`` is kept on the job for the pages to show (e.g. the version being installed)."""
    with _lock:
        job = _jobs.get(app_id)
        if job and job["state"] == RUNNING:
            return False
        _jobs[app_id] = {"label": label, "state": RUNNING, "error": "", "ended": 0.0, "info": dict(info or {})}
    if inline:
        _run(app_id, fn, args, kwargs)
    else:
        threading.Thread(target=_run, args=(app_id, fn, args, kwargs), daemon=True,
                         name=f"app-job-{app_id}").start()
    return True


def _run(app_id, fn, args, kwargs):
    state, error = DONE, ""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        log.warning("%s: job failed: %s", app_id, exc, exc_info=not isinstance(exc, (ValueError,)))
        state, error = FAILED, str(exc) or exc.__class__.__name__
    finally:
        if not inline:
            connections.close_all()  # this thread's own connections
    with _lock:
        _jobs[app_id].update(state=state, error=error, ended=time.monotonic())
