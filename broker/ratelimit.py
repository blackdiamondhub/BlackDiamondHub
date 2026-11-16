"""Per-app token buckets, in memory (one hub process)."""
import threading
import time

# op -> (requests per second, burst)
DEFAULT = (20.0, 60.0)
SLOW = {
    "speakers.announce": (0.2, 3.0),
    "speakers.tts": (0.5, 5.0),
    "ha.get_tts": (0.5, 5.0),
    "ha.upload_media": (0.5, 5.0),
    "ha.ws_commands": (1.0, 5.0),
    "ha.registry": (1.0, 5.0),
    "secrets.get": (2.0, 20.0),
    "secrets.set": (0.5, 5.0),
    "secrets.delete": (0.5, 5.0),
}


class Limiter:
    def __init__(self):
        self._buckets = {}
        self._lock = threading.Lock()

    def _take(self, key, rate, burst, now):
        tokens, last = self._buckets.get(key, (burst, now))
        tokens = min(burst, tokens + (now - last) * rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True

    def allow(self, app_id, op, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if not self._take((app_id, "*"), *DEFAULT, now):
                return False
            if op in SLOW:
                return self._take((app_id, op), *SLOW[op], now)
            return True

    def reset(self):
        with self._lock:
            self._buckets.clear()


limiter = Limiter()
