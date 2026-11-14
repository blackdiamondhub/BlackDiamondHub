import httpx
from asgiref.sync import async_to_sync
from bdh_sdk.identity import verify
from django.contrib.auth.models import User
from django.test import TestCase

from broker import proxy
from broker.tokens import token_for

from .base import make_install, reset_broker


async def _run(app, scope, body=b""):
    inbox = [{"type": "websocket.connect"}] if scope["type"] == "websocket" else \
        [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def run(app, scope, body=b""):
    return async_to_sync(_run)(app, scope, body)


def scope(port, path="/", method="GET", headers=(), query=b"", kind="http"):
    return {"type": kind, "method": method, "path": path, "raw_path": path.encode(),
            "query_string": query, "scheme": "http", "server": ("hub.local", port),
            "client": ("192.168.1.50", 5555),
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


def response(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {}
    for k, v in start["headers"]:
        headers.setdefault(k.decode().lower(), []).append(v.decode())
    return start["status"], headers, body


class Body(httpx.AsyncByteStream):
    """A streamed body, as a real upstream sends (bytes would arrive pre-read)."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class FakeUpstream:
    def __init__(self, status=200, headers=(), body=b"hello", error=None):
        self.requests = []
        self.status, self.headers, self.body, self.error = status, list(headers), body, error

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status, headers=self.headers, stream=Body(self.body))


class ProxyTestCase(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        self.install = make_install()
        self.port = self.install.port
        self.upstream = FakeUpstream()
        old = proxy.transport
        proxy.transport = httpx.MockTransport(self.upstream)
        self.addCleanup(setattr, proxy, "transport", old)

    def hub_cookie(self, **user):
        u = User.objects.create_user("stephen", password="x", first_name="Stephen", **user)
        self.client.force_login(u)
        return self.client.cookies["sessionid"].value


class AppProxyTests(ProxyTestCase):
    def test_forwards_and_signs(self):
        session = self.hub_cookie(is_staff=True)
        headers = [("host", "hub.local:9100"), ("authorization", "Basic abc"), ("x-bdh-identity", "forged"),
                   ("x-bdh-signature", "forged"), ("accept", "text/html"),
                   ("cookie", f"sessionid={session}; csrftoken=hub; bdh_cams_session=s1; bdh_other_session=o")]
        status, _h, body = response(run(proxy.app_proxy, scope(self.port, "/cameras/ü", headers=headers,
                                                                 query=b"a=1&b=2")))
        self.assertEqual((status, body), (200, b"hello"))
        req = self.upstream.requests[0]
        self.assertEqual(str(req.url), "http://127.0.0.1:9201/cameras/%C3%BC?a=1&b=2")
        self.assertEqual(req.headers["cookie"], "bdh_cams_session=s1")
        self.assertNotIn("authorization", req.headers)
        self.assertEqual(req.headers["host"], "hub.local:9100")
        self.assertEqual(req.headers["accept"], "text/html")
        self.assertEqual(req.headers["x-forwarded-for"], "192.168.1.50")
        ident = verify(token_for(self.install), "GET", "/cameras/%C3%BC?a=1&b=2", req.headers)
        self.assertIsNotNone(ident)
        self.assertEqual((ident["u"], ident["n"], ident["staff"]), ("stephen", "Stephen", True))
        self.assertEqual(ident["o"], "http://hub.local:8000")

    def test_anonymous_visitor(self):
        run(proxy.app_proxy, scope(self.port, "/", headers=[("host", "hub.local:9100"),
                                                           ("cookie", "sessionid=bogus")]))
        req = self.upstream.requests[0]
        ident = verify(token_for(self.install), "GET", "/", req.headers)
        self.assertNotIn("u", ident)
        self.assertNotIn("cookie", req.headers)

    def test_post_body(self):
        run(proxy.app_proxy, scope(self.port, "/save/", "POST", [("host", "h:9100")]), b"x=1")
        self.assertEqual(self.upstream.requests[0].content, b"x=1")
        self.assertIsNotNone(verify(token_for(self.install), "POST", "/save/", self.upstream.requests[0].headers))

    def test_response_headers(self):
        self.upstream.headers = [("set-cookie", "bdh_cams_session=new; Path=/"),
                                 ("set-cookie", "sessionid=stolen; Path=/"),
                                 ("x-frame-options", "DENY"), ("content-type", "text/html")]
        _s, headers, _b = response(run(proxy.app_proxy, scope(self.port, "/", headers=[("host", "hub.local:9100")])))
        self.assertEqual(headers["set-cookie"], ["bdh_cams_session=new; Path=/"])
        self.assertNotIn("x-frame-options", headers)
        self.assertEqual(headers["content-security-policy"], ["frame-ancestors http://hub.local:8000"])
        self.assertEqual(headers["content-type"], ["text/html"])

    def test_internal_paths_blocked(self):
        for path in ("/_bdh/hook", "/_bdh", "/_bdh/manifest"):
            status, _h, _b = response(run(proxy.app_proxy, scope(self.port, path, "POST")))
            self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])

    def test_unknown_or_disabled_port(self):
        self.assertEqual(response(run(proxy.app_proxy, scope(self.port + 1)))[0], 404)
        self.install.enabled = False
        self.install.save()
        self.assertEqual(response(run(proxy.app_proxy, scope(self.port)))[0], 404)

    def test_app_down(self):
        self.upstream.error = httpx.ConnectError("refused")
        status, headers, body = response(run(proxy.app_proxy, scope(self.port)))
        self.assertEqual(status, 503)
        self.assertIn(b"cams is starting", body)
        self.upstream.error = httpx.ReadTimeout("slow")
        self.assertEqual(response(run(proxy.app_proxy, scope(self.port)))[0], 502)

    def test_body_cap(self):
        old = proxy.MAX_BODY
        proxy.MAX_BODY = 10
        self.addCleanup(setattr, proxy, "MAX_BODY", old)
        status, _h, _b = response(run(proxy.app_proxy, scope(self.port, "/", "POST"), b"x" * 11))
        self.assertEqual(status, 413)

    def test_websocket_origin_check(self):
        sent = run(proxy.app_proxy, scope(self.port, "/ws/", headers=[("host", "hub.local:9100"),
                                                                      ("origin", "http://hub.local:8000")],
                                          kind="websocket"))
        self.assertEqual(sent, [{"type": "websocket.close", "code": 4403}])
        sent = run(proxy.app_proxy, scope(self.port, "/_bdh/x", kind="websocket"))
        self.assertEqual(sent[-1]["type"], "websocket.close")


class HelperTests(TestCase):
    def test_app_cookies(self):
        self.assertEqual(proxy.app_cookies("a=1; bdh_cams_x=2;bdh_cams_y=3; bdh_camsx_z=4", "bdh_cams_"),
                         "bdh_cams_x=2; bdh_cams_y=3")
        self.assertEqual(proxy.app_cookies(None, "bdh_cams_"), "")


class DispatcherTests(ProxyTestCase):
    def setUp(self):
        super().setUp()
        self.hub_paths = []

        async def hub(scope, receive, send):
            self.hub_paths.append((scope["server"][1], scope["path"]))
            await proxy._plain(send, 200, b"hub")

        self.app = proxy.PortDispatcher(hub)

    def status(self, port, path, kind="http"):
        sent = run(self.app, scope(port, path, kind=kind))
        if kind == "websocket":
            return sent[-1]["type"]
        return response(sent)[0]

    def test_ports(self):
        self.assertEqual(self.status(8000, "/"), 200)
        self.assertEqual(self.status(8000, "/_broker/rpc/hub.brand"), 404)
        self.assertEqual(self.status(8000, "/_broker"), 404)
        self.assertEqual(self.status(8099, "/_broker/rpc/hub.brand"), 200)
        self.assertEqual(self.status(8099, "/"), 404)
        self.assertEqual(self.status(8099, "/admin/"), 404)
        self.assertEqual(self.status(8099, "/_broker/rpc/x", "websocket"), "websocket.close")
        self.assertEqual(self.status(self.port, "/_broker/rpc/hub.brand"), 404)
        self.assertEqual(self.status(self.port, "/"), 200)
        self.assertEqual(self.hub_paths, [(8000, "/"), (8099, "/_broker/rpc/hub.brand")])
        self.assertEqual([r.url.path for r in self.upstream.requests], ["/"])
