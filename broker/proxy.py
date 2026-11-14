"""The hub's front door: one ASGI app on every port daphne listens on.

``scope["server"][1]`` (the listener's port) picks what serves a request:

* the hub port (8000) and anything unknown: the hub itself, except ``/_broker``;
* the broker port (``BROKER_PORT``, 8099, bound to the docker bridge only):
  ``/_broker/`` over HTTP and nothing else;
* an app port (``APP_PORT_BASE`` + n): a reverse proxy to that app's container.

The proxy is the only way in to an app. It forwards only the app's own cookies,
drops ``Authorization`` and any ``X-BDH-*`` headers, and signs who the visitor
is (from the hub's session cookie) with the app's token. Responses may only
set the app's own cookies and may only be framed by the hub.
"""
import asyncio
import html
import logging
from http.cookies import SimpleCookie

import httpx
from asgiref.sync import sync_to_async
from bdh_sdk.identity import signed_headers
from django.conf import settings
from django.http.request import validate_host
from django.utils.encoding import escape_uri_path, iri_to_uri

from . import conf, lifecycle, remote

log = logging.getLogger(__name__)

MAX_BODY = 16 * 1024 * 1024
TIMEOUT = httpx.Timeout(30.0, connect=3.0)
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
DROP_REQUEST = HOP_BY_HOP | {"authorization", "cookie", "forwarded", "x-forwarded-for",
                             "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}
DROP_RESPONSE = HOP_BY_HOP | {"x-frame-options", "set-cookie"}
# The websocket client library writes its own handshake headers.
WS_HANDSHAKE = frozenset({"host", "origin", "sec-websocket-key", "sec-websocket-version",
                          "sec-websocket-extensions", "sec-websocket-protocol"})

# Tests swap in an httpx.MockTransport.
transport = None

STARTING_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3"><title>Starting…</title>
<style>body{{font-family:sans-serif;background:#0d1117;color:#c9d1d9;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}</style></head>
<body><p>{name} is starting…</p></body></html>"""


def _is_broker_path(path):
    return path == "/_broker" or path.startswith("/_broker/")


def _is_internal_path(path):
    return path == "/_bdh" or path.startswith("/_bdh/")


async def _plain(send, status, body=b"", content_type=b"text/plain; charset=utf-8", extra=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type),
                            (b"content-length", str(len(body)).encode()), *extra]})
    await send({"type": "http.response.body", "body": body})


async def _refuse(scope, receive, send, status=404, body=b"Not found"):
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 4404 if status == 404 else 4403})
        return
    await _plain(send, status, body)


class PortDispatcher:
    """Sends each connection to the hub, the broker or an app by listener port."""

    def __init__(self, hub_app):
        self.hub_app = hub_app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.hub_app(scope, receive, send)
        port = (scope.get("server") or (None, None))[1]
        path = scope.get("path") or ""
        if port is not None and conf.is_app_port(port):
            if _is_broker_path(path):
                return await _refuse(scope, receive, send)
            return await app_proxy(scope, receive, send)
        if port is not None and int(port) == conf.broker_port():
            if scope["type"] == "http" and path.startswith("/_broker/"):
                return await self.hub_app(scope, receive, send)
            return await _refuse(scope, receive, send)
        if _is_broker_path(path):
            return await _refuse(scope, receive, send)
        return await self.hub_app(scope, receive, send)


# --- the app proxy -------------------------------------------------------------

def _headers(scope):
    return [(k.decode("latin1").lower(), v.decode("latin1")) for k, v in scope.get("headers") or ()]


def _header(headers, name):
    return next((v for k, v in headers if k == name), "")


def app_cookies(cookie_header, names):
    """Only the cookies in ``names`` (the app's own)."""
    kept = []
    for part in (cookie_header or "").split(";"):
        name = part.split("=", 1)[0].strip()
        if name in names:
            kept.append(part.strip())
    return "; ".join(kept)


def hub_session(cookie_header):
    jar = SimpleCookie()
    try:
        jar.load(cookie_header or "")
    except Exception:
        return ""
    morsel = jar.get(settings.SESSION_COOKIE_NAME)
    return morsel.value if morsel else ""


def signed_path(scope):
    """The path as the app's Django sees it (``request.get_full_path()``)."""
    path = escape_uri_path(scope.get("path") or "/")
    query = (scope.get("query_string") or b"").decode("utf-8", "replace")
    return path + ("?" + iri_to_uri(query) if query else "")


async def _identity(scope, headers):
    from .visitor import for_session

    return await sync_to_async(for_session)(hub_session(_header(headers, "cookie")),
                                            _header(headers, "host"))


def _client_ip(scope):
    client = scope.get("client") or ("", 0)
    return client[0] or ""


async def _request_headers(scope, headers, install, method):
    ident = await _identity(scope, headers)
    out = [(k, v) for k, v in headers if k not in DROP_REQUEST and not k.startswith("x-bdh-")]
    cookies = app_cookies(_header(headers, "cookie"), install.cookie_names)
    if cookies:
        out.append(("cookie", cookies))
    # The app's CSRF check compares Origin with Host, so it sees the public host.
    host = _header(headers, "host")
    if host:
        out.append(("host", host))
    out += [("x-forwarded-for", _client_ip(scope)), ("x-forwarded-proto", scope.get("scheme") or "http")]
    token = await sync_to_async(remote._token)(install)
    out += list(signed_headers(token, method, signed_path(scope), identity=ident).items())
    return out


def response_headers(raw_headers, install, host):
    """Upstream response headers made safe to hand to the visitor."""
    out = []
    for key, value in raw_headers:
        name = key.decode("latin1").lower() if isinstance(key, bytes) else key.lower()
        raw = value if isinstance(value, bytes) else value.encode("latin1")
        if name == "set-cookie":
            cookie = raw.decode("latin1").split("=", 1)[0].strip()
            if cookie in install.cookie_names:
                out.append((b"set-cookie", raw))
            continue
        if name in DROP_RESPONSE:
            continue
        out.append((name.encode("latin1"), raw))
    out.append((b"content-security-policy",
                f"frame-ancestors {remote.hub_origin(host)}".encode("latin1")))
    return out


async def _read_body(receive):
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > MAX_BODY:
            return False
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


async def app_proxy(scope, receive, send):
    port = scope["server"][1]
    path = scope.get("path") or "/"
    if _is_internal_path(path):
        return await _refuse(scope, receive, send, 403, b"Forbidden")
    # The hub port gets this from Django; without it a DNS-rebinding page could drive apps.
    if not validate_host(remote.hostname(_header(_headers(scope), "host")), settings.ALLOWED_HOSTS):
        return await _refuse(scope, receive, send, 400, b"Bad Request")
    install = await sync_to_async(remote.install_for_port)(port)
    if install is None or not install.upstream:
        return await _refuse(scope, receive, send)
    if not await lifecycle.ensure_running(install):
        return await _starting(scope, receive, send, install)
    lifecycle.touch(install)
    if scope["type"] == "websocket":
        return await _websocket(scope, receive, send, install)
    return await _http(scope, receive, send, install)


async def _starting(scope, receive, send, install):
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 1013})
        return
    page = STARTING_PAGE.format(name=html.escape(install.app_id)).encode()
    await _plain(send, 503, page, b"text/html; charset=utf-8", [(b"retry-after", b"3")])


async def _http(scope, receive, send, install):
    headers = _headers(scope)
    method = scope.get("method") or "GET"
    body = await _read_body(receive)
    if body is None:
        return
    if body is False:
        return await _plain(send, 413, b"Request too large")
    out = await _request_headers(scope, headers, install, method)
    url = install.upstream.rstrip("/") + signed_path(scope)
    host = _header(headers, "host")
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT) as client:
            request = client.build_request(method, url, headers=out, content=body or None)
            resp = await client.send(request, stream=True)
            try:
                await send({"type": "http.response.start", "status": resp.status_code,
                            "headers": response_headers(resp.headers.raw, install, host)})
                async for chunk in resp.aiter_raw():
                    if chunk:
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})
            finally:
                await resp.aclose()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        log.info("%s is down: %s", install.app_id, exc)
        lifecycle.forget(install.app_id)
        await _starting(scope, receive, send, install)
    except httpx.HTTPError as exc:
        log.warning("%s proxy error: %s", install.app_id, exc)
        await _plain(send, 502, b"Bad gateway")


# --- websockets ----------------------------------------------------------------

def _ws_connect(url, headers, subprotocols):
    try:
        from websockets.asyncio.client import connect
        return connect(url, additional_headers=headers, subprotocols=subprotocols or None,
                       open_timeout=5, max_size=MAX_BODY)
    except ImportError:  # websockets < 13
        import websockets
        return websockets.connect(url, extra_headers=headers, subprotocols=subprotocols or None,
                                  open_timeout=5, max_size=MAX_BODY)


async def _websocket(scope, receive, send, install):
    headers = _headers(scope)
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    host = _header(headers, "host")
    if _header(headers, "origin") != remote.app_origin(scope["server"][1], host):
        await send({"type": "websocket.close", "code": 4403})
        return
    out = [(k, v) for k, v in await _request_headers(scope, headers, install, "GET")
           if k not in WS_HANDSHAKE]
    out.append(("origin", _header(headers, "origin")))
    url = "ws" + install.upstream.rstrip("/")[len("http"):] + signed_path(scope)
    subprotocols = list(scope.get("subprotocols") or ())
    try:
        upstream = await _ws_connect(url, out, subprotocols)
    except Exception as exc:
        log.info("%s websocket refused: %s", install.app_id, exc)
        lifecycle.forget(install.app_id)
        await send({"type": "websocket.close", "code": 1013})
        return
    try:
        await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

        async def client_to_app():
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])

        async def app_to_client():
            async for data in upstream:
                if isinstance(data, bytes):
                    await send({"type": "websocket.send", "bytes": data})
                else:
                    await send({"type": "websocket.send", "text": data})
            await send({"type": "websocket.close", "code": 1000})

        tasks = [asyncio.ensure_future(client_to_app()), asyncio.ensure_future(app_to_client())]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except Exception as exc:
        log.debug("%s websocket ended: %s", install.app_id, exc)
    finally:
        await upstream.close()
