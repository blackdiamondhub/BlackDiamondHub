"""One asyncio loop in a daemon thread for the speaker drivers.

Started on first use, so only the process that announces (Daphne, or a
management command testing a speaker) ever runs it. Sync callers hand it a
coroutine with :func:`run`. Nothing on the loop touches the database:
Django refuses ORM calls from a thread with a running loop, so callers do
their database work first and blocking calls (Home Assistant, soco) go to
:func:`blocking`, which runs them in a worker thread. :func:`close` (also
run at exit) closes the aiohttp session and stops the loop; the next use
starts a new one.
"""
import asyncio
import atexit
import threading

from django.db import close_old_connections

_loop = None
_lock = threading.Lock()
_session = None


def loop():
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            ready = threading.Event()
            new = asyncio.new_event_loop()

            def main():
                asyncio.set_event_loop(new)
                ready.set()
                try:
                    new.run_forever()
                finally:
                    new.close()

            threading.Thread(target=main, name="hub-speakers", daemon=True).start()
            ready.wait()
            _loop = new
            atexit.register(close)
        return _loop


def close(timeout=5):
    """Close the shared session and stop the loop (at exit, or after one-off use in a shell)."""
    global _loop
    with _lock:
        current, _loop = _loop, None
    atexit.unregister(close)
    if current is None or current.is_closed() or not current.is_running():
        return

    async def finish():
        global _session
        session, _session = _session, None
        if session is not None and not session.closed:
            await session.close()

    try:
        asyncio.run_coroutine_threadsafe(finish(), current).result(timeout)
    except Exception:  # noqa: BLE001 - shutting down regardless
        pass
    current.call_soon_threadsafe(current.stop)


def run(coro, timeout):
    """Run ``coro`` on the loop and wait for it; raises TimeoutError after ``timeout``."""
    future = asyncio.run_coroutine_threadsafe(coro, loop())
    try:
        return future.result(timeout)
    except TimeoutError:
        future.cancel()
        raise


async def session():
    """The loop's shared aiohttp session (made on the loop, kept for its life)."""
    import aiohttp

    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def blocking(fn, *args):
    """``fn(*args)`` in a worker thread, closing stale DB connections around it."""
    def call():
        close_old_connections()
        try:
            return fn(*args)
        finally:
            close_old_connections()
    return await asyncio.get_running_loop().run_in_executor(None, call)
