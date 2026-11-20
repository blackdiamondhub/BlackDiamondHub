"""Event sources: the hub end of ``bdh_sdk.events``.

Each ``[events.sources.<name>]`` table in ``site.toml`` is a source that
publishes topics starting with ``<name>/``:

Home Assistant instances
    Every instance on Settings > Home Assistant gets a source named after
    its id, kept in step by ``sync_instances()``. A ``home_assistant`` table
    in ``site.toml`` doesn't add a source of its own.

``type = "home_assistant"``
    One websocket to Home Assistant. Publishes ``<name>/state/<entity_id>``
    with ``{"entity_id", "old", "new", "attributes"}``. It asks Home
    Assistant only for the entities apps subscribed to; a wildcard pattern
    that could match any entity switches it to every state change.
    Reconnects on its own; changes while disconnected are lost.

``type = "webhook"``
    ``POST /hub/api/events/hook/<name>/<topic...>`` with header
    ``X-Hub-Secret: <value of secret_env>`` publishes ``<name>/<topic...>``
    with the JSON body (or ``None``) as data. Disabled while the secret is unset.
"""
import json
import logging
import os
import sys
import threading
import time

from bdh_sdk import events

log = logging.getLogger(__name__)

TYPES = ("home_assistant", "webhook")
_PROBE = "zz_probe.zz_probe"

_sources = {}
_config = {}
_started = False
_sync_lock = threading.Lock()


def should_start(argv=None):
    """Run sources only in the server process (Daphne, or runserver's reloaded child)."""
    if os.environ.get("HUB_EVENTS", "").lower() in ("0", "false", "off", "no"):
        return False
    return is_server(argv)


def is_server(argv=None):
    """True in the process that serves requests (Daphne, or runserver's reloaded child)."""
    argv = sys.argv if argv is None else argv
    prog = os.path.basename(argv[0]) if argv else ""
    if "daphne" in prog:
        return True
    if len(argv) > 1 and prog in ("manage.py", "django-admin", "__main__.py") and argv[1] == "runserver":
        return os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv
    return False


def start(sources):
    """Start the event sources: the site's config now, the instances shortly after."""
    global _started
    _config.update(sources)
    _started = True


def reserved_names():
    """Source names that aren't Home Assistant instances (e.g. webhooks)."""
    return {name for name, conf in _config.items() if conf["type"] != "home_assistant"}


def sync_instances(delay=0):
    """Start, stop or reconnect sources to match the Home Assistant instances.

    No-op unless ``start()`` ran in this process. With ``delay`` it runs in a
    thread after that many seconds (the database isn't usable during startup).
    """
    if not _started:
        return
    if delay:
        timer = threading.Timer(delay, sync_instances)
        timer.daemon = True
        timer.start()
        return
    from hub_settings import ha

    try:
        wanted = [i["id"] for i in ha.instances()]
    except Exception:
        log.exception("event sources: can't read the Home Assistant instances")
        return
    reserved = reserved_names()
    with _sync_lock:
        for name, source in list(_sources.items()):
            if source.managed and name not in wanted:
                source.stop()
                del _sources[name]
        for name in wanted:
            source = _sources.get(name)
            if source is not None:
                if source.managed:
                    source.refresh()
                continue
            if name in reserved:
                log.warning("event source %s: the name is taken; not started", name)
                continue
            source = HomeAssistantSource(name, conn=lambda name=name: ha.connection(name))
            _sources[name] = source
            source.start()


def instance_status(name):
    """The live event source's ``status`` for an instance, or ``None`` if none runs here."""
    source = _sources.get(name)
    return source.status if isinstance(source, HomeAssistantSource) else None


def wanted_entities(name, patterns):
    """Entity ids source ``name`` must watch, or ``None`` for every entity."""
    entities = set()
    for pattern in patterns:
        parts = pattern.split("/")
        if len(parts) == 3 and parts[:2] == [name, "state"] and "+" not in parts[2] and "#" not in parts[2]:
            entities.add(parts[2])
        elif events.matches(pattern, f"{name}/state/{_PROBE}"):
            return None
    return entities


def state_event(entity_id, old, new):
    return {
        "entity_id": entity_id,
        "old": (old or {}).get("state"),
        "new": (new or {}).get("state"),
        "attributes": (new or {}).get("attributes", {}),
    }


class HomeAssistantSource:
    RETRY_MIN, RETRY_MAX = 5, 60
    IDLE = 30  # seconds without traffic before a ping
    # After a drop, say "reconnecting" this long (a Home Assistant restart) before "unreachable".
    RECONNECTING = 180
    # status: connecting, connected, reconnecting, unreachable, bad_token, no_token or idle (no
    # subscribers, so no connection to judge by).

    def __init__(self, name, url=None, token=None, conn=None):
        """``conn`` returns ``(url, token)``; it's asked again on every connect."""
        self.name = name
        self.managed = conn is not None
        self._conn = conn or (lambda: (url, token))
        if url is not None:
            self.url, self.token = self._target(url, token)
        self.entities = set()
        self.status = "connecting"
        self._lost_at = None
        self._ws = None
        self._current = None
        self._stopped = False
        self._unlisten = None
        self._lock = threading.Lock()

    @staticmethod
    def _target(url, token):
        return url.rstrip("/").replace("http", "ws", 1) + "/api/websocket", token

    def start(self):
        self._unlisten = events.add_listener(self._patterns_changed)
        threading.Thread(target=self._run, name=f"events-{self.name}", daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._unlisten:
            self._unlisten()
        self._close()

    def refresh(self):
        """Reconnect if the instance's url or token changed."""
        try:
            target = self._target(*self._conn())
        except Exception:
            target = None
        with self._lock:
            changed = self._current is not None and target != self._current
        if changed:
            self._close()

    def _close(self):
        with self._lock:
            ws = self._ws
        if ws is not None:
            ws.close()

    def _patterns_changed(self, patterns):
        wanted = wanted_entities(self.name, patterns)
        with self._lock:
            if wanted == self.entities:
                return
            self.entities = wanted
            ws = self._ws
        if ws is not None:
            ws.close()  # the reader reconnects with the new subscription

    def _run(self):
        time.sleep(2)  # let apps subscribe before the first connect
        delay = self.RETRY_MIN
        while not self._stopped:
            try:
                self._session()
                delay = self.RETRY_MIN
            except Exception as exc:
                if self._stopped:
                    break
                self._failed(exc)
                log.warning("event source %s: %s; retrying in %ss", self.name, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX)

    def _session(self):
        import websocket

        with self._lock:
            entities = self.entities
        if entities == set():
            self.status, self._lost_at = "idle", None
            time.sleep(self.IDLE)  # nobody subscribed; check again later
            return
        url, token = target = self._target(*self._conn())
        if not token:
            raise ConnectionError("no access token")
        ws = websocket.create_connection(url, timeout=10)
        try:
            if json.loads(ws.recv()).get("type") != "auth_required":
                raise ConnectionError("unexpected greeting")
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise ConnectionError("authentication failed")
            if entities is None:
                ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            else:
                ws.send(json.dumps({"id": 1, "type": "subscribe_trigger", "trigger": {
                    "platform": "state", "entity_id": sorted(entities)}}))
            with self._lock:
                if entities != self.entities or self._stopped:
                    return  # changed or stopped while connecting
                self._ws = ws
                self._current = target
                self.status, self._lost_at = "connected", None
            log.info("event source %s: connected, watching %s", self.name,
                     "every entity" if entities is None else ", ".join(sorted(entities)))
            self._read(ws)
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws = None
                if self.status == "connected":
                    self.status, self._lost_at = "reconnecting", time.monotonic()
            ws.close()

    def _failed(self, exc):
        """Set ``status`` after a failed connect or a dropped session."""
        if str(exc) == "no access token":
            self.status, self._lost_at = "no_token", None
        elif str(exc) == "authentication failed":
            self.status, self._lost_at = "bad_token", None
        elif self._lost_at is not None and time.monotonic() - self._lost_at < self.RECONNECTING:
            self.status = "reconnecting"
        else:
            self.status, self._lost_at = "unreachable", None

    def _read(self, ws):
        import websocket

        ws.settimeout(self.IDLE)
        next_id, waiting = 2, False
        while True:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if waiting:
                    raise ConnectionError("no pong")
                ws.send(json.dumps({"id": next_id, "type": "ping"}))
                next_id, waiting = next_id + 1, True
                continue
            if not raw:
                if not ws.connected:
                    return
                continue
            waiting = False
            self._handle(json.loads(raw))

    def _handle(self, msg):
        if msg.get("type") == "result" and not msg.get("success", True):
            raise ConnectionError(f"subscribe failed: {msg.get('error')}")
        if msg.get("type") != "event":
            return
        event = msg.get("event", {})
        if "variables" in event:  # subscribe_trigger
            trigger = event["variables"].get("trigger", {})
            entity_id, old, new = trigger.get("entity_id"), trigger.get("from_state"), trigger.get("to_state")
        else:  # state_changed
            data = event.get("data", {})
            entity_id, old, new = data.get("entity_id"), data.get("old_state"), data.get("new_state")
        if entity_id:
            events.publish(f"{self.name}/state/{entity_id}", state_event(entity_id, old, new))
