"""The push channel: tells an app's open pages which of its topics changed.

A page on an app's port opens ``/_bdh/push`` (same origin, so the hub session
cookie comes along). The hub asks the app, signed and as that visitor, which
of its own topics they may hear (``GET /_bdh/push/authorize``), then sends
``{"t":"inv","topics":[...]}`` whenever the app publishes a matching topic.
Only topic names go out; the page refetches through the app, which keeps
enforcing access. All state lives on daphne's event loop; the bus calls in
from its dispatcher thread through ``call_soon_threadsafe``.
"""
import asyncio
import collections
import itertools
import json
import logging
import threading
import time

from asgiref.sync import sync_to_async
from bdh_sdk import events as bus
from django.conf import settings
from django.http.request import validate_host

from . import remote

log = logging.getLogger(__name__)

PROTO = 1
INFO_PATH = "/_bdh/push/info"
WS_PATH = "/_bdh/push"
AUTHORIZE_PATH = "/_bdh/push/authorize"

MAX_PER_IP = 8
MAX_TOTAL = 200
MAX_PATTERNS = 16
QUEUE = 64              # distinct pending topics before a connection is told to resync
PING_EVERY = 20.0
SILENT_FOR = 75.0       # no pong or stats for this long: the page is gone
SEND_TIMEOUT = 10.0
RECHECK_EVERY = 300.0
AUTH_TIMEOUT = 3.0
MAX_CLIENT_MESSAGE = 4096
KEEP_CLOSED = 50
KEEP_FRAMES = 100

# Close codes (push.js knows them).
GONE = 1000
APP_DOWN = 1013
SILENT = 4000
REFUSED = 4403
NO_PUSH = 4404
STALLED = 4408
BUSY = 4429
KICKED = 4499

_ids = itertools.count(1)
_lock = threading.Lock()   # guards everything below against the stats page's thread
_conns = {}                # id -> Conn
_subs = {}                 # app_id -> Subscription
_admitting = collections.Counter()  # ip -> connections being authorized
_closed = collections.deque(maxlen=KEEP_CLOSED)
_frames = collections.deque(maxlen=KEEP_FRAMES)
_topics = collections.Counter()     # topic -> times published while someone listened
_codes = collections.Counter()      # close/refuse code -> count
_totals = collections.Counter()
_started = time.time()


def is_push_path(path):
    return path in (INFO_PATH, WS_PATH)


def _filter(app_id, patterns):
    """``patterns`` limited to valid ones under ``<app_id>/``."""
    if not isinstance(patterns, list):
        return []
    out = set()
    for p in patterns:
        if not isinstance(p, str) or not p.startswith(app_id + "/"):
            continue
        try:
            bus._segments(p, wildcards=True)
        except ValueError:
            continue
        out.add(p)
    return sorted(out)[:MAX_PATTERNS]


class Down(Exception):
    """The app couldn't answer."""


def _authorize(install, identity):
    """Patterns ``identity`` may hear: None if the app has no push, [] if none (sync)."""
    try:
        resp = remote.signed_request(install, "GET", AUTHORIZE_PATH, None, timeout=AUTH_TIMEOUT,
                                     identity=identity, wake=False)
    except Exception as exc:
        raise Down(str(exc)) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        return []
    if resp.status_code != 200:
        raise Down(f"HTTP {resp.status_code}")
    try:
        return _filter(install.app_id, resp.json().get("patterns"))
    except (ValueError, AttributeError) as exc:
        raise Down("bad reply") from exc


def _still_installed(app_id, port):
    install = remote.install_for_port(port)
    return install if install is not None and install.app_id == app_id and install.upstream else None


class Conn:
    def __init__(self, install, port, ip, identity, patterns, agent):
        self.id = next(_ids)
        self.app_id = install.app_id
        self.install = install
        self.port = port
        self.ip = ip
        self.identity = identity
        self.user = (identity or {}).get("u") or "guest"
        self.patterns = patterns
        self.agent = agent[:160]
        self.loop = asyncio.get_running_loop()
        self.pending = {}              # topic -> first seen (monotonic), insertion ordered
        self.resync = False
        self.wake = asyncio.Event()
        self.kick = None               # close code asked for from the stats page
        self.connected_at = time.time()
        self.last_seen = time.monotonic()
        self.client = {}
        self.counts = collections.Counter()

    def offer(self, topic):
        """On the loop: queue ``topic`` if this visitor may hear it."""
        if not any(bus.matches(p, topic) for p in self.patterns):
            return
        self.counts["matched"] += 1
        if topic in self.pending:
            self.counts["coalesced"] += 1
        elif len(self.pending) >= QUEUE:
            self.pending.clear()
            self.resync = True
            self.counts["overflows"] += 1
        else:
            self.pending[topic] = time.monotonic()
        self.wake.set()

    def snapshot(self):
        return {
            "id": self.id, "app": self.app_id, "ip": self.ip, "user": self.user, "agent": self.agent,
            "patterns": list(self.patterns), "connected_at": self.connected_at,
            "idle": round(time.monotonic() - self.last_seen, 1), "queued": len(self.pending),
            "counts": dict(self.counts), "client": dict(self.client),
        }


# --- the bus -------------------------------------------------------------------

def _fanout(app_id, topic):
    with _lock:
        conns = [c for c in _conns.values() if c.app_id == app_id]
        if conns:
            _topics[topic] += 1
            _totals["published"] += 1
    for conn in conns:
        conn.offer(topic)


def _handler(app_id, loop):
    def relay(event):
        try:
            loop.call_soon_threadsafe(_fanout, app_id, event.topic)
        except RuntimeError:  # the loop closed (shutdown)
            pass
    relay.__qualname__ = f"push[{app_id}]"
    return relay


def _add(conn):
    with _lock:
        _conns[conn.id] = conn
        if conn.app_id not in _subs:
            _subs[conn.app_id] = bus.subscribe(f"{conn.app_id}/#", _handler(conn.app_id, conn.loop))
        _totals["connects"] += 1


def _remove(conn, code, reason):
    sub = None
    with _lock:
        _conns.pop(conn.id, None)
        if not any(c.app_id == conn.app_id for c in _conns.values()):
            sub = _subs.pop(conn.app_id, None)
        info = conn.snapshot()
        info.update(closed_at=time.time(), code=code, reason=reason)
        _closed.appendleft(info)
        _codes[code] += 1
    if sub is not None:
        sub.cancel()


def _refused(code):
    with _lock:
        _codes[code] += 1
        _totals["refused"] += 1


# --- HTTP: /_bdh/push/info ------------------------------------------------------

async def info(scope, receive, send, headers):
    from .proxy import _header, _plain

    if not validate_host(remote.hostname(_header(headers, "host")), settings.ALLOWED_HOSTS):
        return await _plain(send, 400, b"Bad Request")
    if scope.get("method") not in ("GET", "HEAD"):
        return await _plain(send, 405, b"Method not allowed")
    install = await sync_to_async(remote.install_for_port)(scope["server"][1])
    if install is None or not install.upstream:
        return await _plain(send, 404, b"Not found")
    body = json.dumps({"proto": PROTO}).encode()
    await _plain(send, 200, body, b"application/json", [(b"cache-control", b"no-store")])


# --- the websocket --------------------------------------------------------------

class _Close(Exception):
    def __init__(self, code, reason=""):
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def websocket(scope, receive, send, headers):
    from .proxy import _client_ip, _header, _identity

    message = await receive()
    if message["type"] != "websocket.connect":
        return
    port = scope["server"][1]
    host = _header(headers, "host")

    async def refuse(code, why):
        _refused(code)
        log.debug("push refused on %s: %s", port, why)
        await send({"type": "websocket.close", "code": code})

    if not validate_host(remote.hostname(host), settings.ALLOWED_HOSTS):
        return await refuse(REFUSED, "host")
    if _header(headers, "origin") != remote.app_origin(port, host):
        return await refuse(REFUSED, "origin")
    install = await sync_to_async(remote.install_for_port)(port)
    if install is None or not install.upstream:
        return await refuse(NO_PUSH, "no app")
    ip = _client_ip(scope)
    with _lock:
        mine = sum(1 for c in _conns.values() if c.ip == ip) + _admitting[ip]
        total = len(_conns) + sum(_admitting.values())
        busy = mine >= MAX_PER_IP or total >= MAX_TOTAL
        if not busy:
            _admitting[ip] += 1
    if busy:
        return await refuse(BUSY, "busy")
    try:
        identity = await _identity(scope, headers)
        try:
            patterns = await sync_to_async(_authorize, thread_sensitive=False)(install, identity)
        except Down as exc:
            log.info("push for %s: app didn't answer: %s", install.app_id, exc)
            return await refuse(APP_DOWN, "down")
        if patterns is None:
            return await refuse(NO_PUSH, "app has no push")
        if not patterns:
            return await refuse(REFUSED, "not allowed")
        conn = Conn(install, port, ip, identity, patterns, _header(headers, "user-agent"))
        _add(conn)
    finally:
        with _lock:
            _admitting[ip] -= 1
            if _admitting[ip] <= 0:
                del _admitting[ip]

    code, reason = GONE, "closed"
    tasks = []
    try:
        await send({"type": "websocket.accept"})
        await _out(conn, send, {"t": "hello", "proto": PROTO, "patterns": conn.patterns})
        tasks = [asyncio.ensure_future(t) for t in
                 (_reader(conn, receive), _writer(conn, send), _recheck(conn))]
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        exc = next(iter(done)).exception()
        if isinstance(exc, _Close):
            code, reason = exc.code, exc.reason
        elif exc is not None:
            code, reason = 1011, type(exc).__name__
            log.warning("push connection %s failed: %r", conn.id, exc)
    except _Close as exc:
        code, reason = exc.code, exc.reason
    finally:
        for task in tasks:
            task.cancel()
        _remove(conn, code, reason)
    if reason != "gone":
        try:
            await send({"type": "websocket.close", "code": code})
        except Exception:
            pass


async def _out(conn, send, message):
    text = json.dumps(message, separators=(",", ":"))
    try:
        await asyncio.wait_for(send({"type": "websocket.send", "text": text}), SEND_TIMEOUT)
    except asyncio.TimeoutError:
        raise _Close(STALLED, "stalled") from None
    conn.counts["frames"] += 1
    with _lock:
        _frames.appendleft({"at": time.time(), "conn": conn.id, "app": conn.app_id, "dir": "out",
                            "msg": text[:300]})


def _clean_stats(raw):
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in list(raw.items())[:24]:
        if isinstance(key, str) and len(key) <= 32 and isinstance(value, (int, float, str, bool)):
            out[key] = value[:80] if isinstance(value, str) else value
    return out


async def _reader(conn, receive):
    while True:
        message = await receive()
        if message["type"] == "websocket.disconnect":
            raise _Close(message.get("code") or GONE, "gone")
        text = message.get("text")
        if text is None or len(text) > MAX_CLIENT_MESSAGE:
            raise _Close(1009, "bad frame")
        try:
            data = json.loads(text)
        except ValueError:
            raise _Close(1007, "bad frame") from None
        conn.last_seen = time.monotonic()
        kind = data.get("t") if isinstance(data, dict) else None
        if kind == "stats":
            conn.client = _clean_stats(data.get("s"))
            conn.client["at"] = time.time()
        conn.counts["received"] += 1
        with _lock:
            _frames.appendleft({"at": time.time(), "conn": conn.id, "app": conn.app_id, "dir": "in",
                                "msg": text[:300]})


async def _writer(conn, send):
    while True:
        try:
            await asyncio.wait_for(conn.wake.wait(), PING_EVERY)
        except asyncio.TimeoutError:
            if time.monotonic() - conn.last_seen > SILENT_FOR:
                raise _Close(SILENT, "silent") from None
            await _out(conn, send, {"t": "ping", "at": time.time()})
            continue
        conn.wake.clear()
        if conn.kick:
            raise _Close(conn.kick, "disconnected by staff")
        if conn.resync:
            conn.resync = False
            conn.pending.clear()
            conn.counts["resyncs"] += 1
            await _out(conn, send, {"t": "resync"})
        elif conn.pending:
            now = time.monotonic()
            conn.counts["lag_ms"] = round((now - min(conn.pending.values())) * 1000)
            topics = list(conn.pending)
            conn.pending.clear()
            conn.counts["topics"] += len(topics)
            await _out(conn, send, {"t": "inv", "topics": topics})


async def _recheck(conn):
    """Ask the app again now and then; the visitor may have lost access."""
    while True:
        await asyncio.sleep(RECHECK_EVERY)
        install = await sync_to_async(_still_installed)(conn.app_id, conn.port)
        if install is None:
            raise _Close(NO_PUSH, "app gone")
        conn.install = install
        try:
            patterns = await sync_to_async(_authorize, thread_sensitive=False)(install, conn.identity)
        except Down:
            continue  # the app is restarting; keep the socket and ask next time
        if patterns is None:
            raise _Close(NO_PUSH, "app dropped push")
        if not patterns:
            raise _Close(REFUSED, "no longer allowed")
        conn.patterns = patterns


# --- staff tools (the stats page) -----------------------------------------------

def stats():
    with _lock:
        conns = [c.snapshot() for c in _conns.values()]
        return {
            "proto": PROTO, "uptime": round(time.time() - _started), "now": time.time(),
            "totals": dict(_totals), "codes": {str(k): v for k, v in _codes.most_common()},
            "subscriptions": sorted(_subs), "connections": sorted(conns, key=lambda c: c["id"]),
            "closed": list(_closed), "topics": _topics.most_common(30), "frames": list(_frames)[:50],
            "limits": {"per_ip": MAX_PER_IP, "total": MAX_TOTAL, "queue": QUEUE},
        }


def _poke(conn_id, how):
    with _lock:
        conn = _conns.get(conn_id)
    if conn is None:
        return False

    def go():
        if how == "kick":
            conn.kick = KICKED
        else:
            conn.resync = True
        conn.wake.set()
    conn.loop.call_soon_threadsafe(go)
    return True


def force_resync(conn_id):
    return _poke(conn_id, "resync")


def disconnect(conn_id):
    return _poke(conn_id, "kick")


def reset():
    """Tests: forget everything."""
    with _lock:
        subs = list(_subs.values())
        _conns.clear()
        _subs.clear()
        _admitting.clear()
        _closed.clear()
        _frames.clear()
        _topics.clear()
        _codes.clear()
        _totals.clear()
    for sub in subs:
        sub.cancel()
