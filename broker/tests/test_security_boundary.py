"""Phase 10: what a hostile container app tries against the hub, and why it fails.

The container side (docker/supervisor sockets, read-only rootfs, pids and
memory limits, egress, forged identity headers at the app port) is in
bdh-supervisor's tests/test_integration_docker.py. Other hub-side cases are
in test_proxy.py (cookie/Authorization stripping, CSP frame-ancestors, /_bdh,
body cap, Host checks) and test_rpc.py (TokenTests: forged, missing, rotated
and other apps' tokens; OwnDataTests and the grant tests: ops without grants).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from broker import proxy

from .base import make_install, reset_broker
from .test_proxy import ProxyTestCase, response, run, scope

BROKER = Path(__file__).resolve().parents[1]
EMBED_HOST = BROKER / "static" / "broker" / "embed-host.js"
HARNESS = Path(__file__).with_name("embed_host_harness.js")
APP_ORIGIN = "http://hub.local:9100"


class SmuggledHeaderTests(ProxyTestCase):
    def test_underscore_spellings_are_dropped(self):
        # Django folds X_Forwarded_For and X-Forwarded-For into one META key, so
        # an underscore spelling would reach the app as the client IP.
        headers = [("host", "hub.local:9100"), ("x_forwarded_for", "6.6.6.6"), ("x_real_ip", "6.6.6.6"),
                   ("x_forwarded_host", "evil.example"), ("x_bdh_identity", "forged"),
                   ("x_bdh_signature", "forged"), ("authorization", "Basic abc"), ("accept", "text/html")]
        status, _h, _b = response(run(proxy.app_proxy, scope(self.port, "/", headers=headers)))
        self.assertEqual(status, 200)
        req = self.upstream.requests[0]
        names = [k.decode().lower() for k, _v in req.headers.raw]
        self.assertFalse([n for n in names if "_" in n], names)
        self.assertEqual(req.headers.get_list("x-forwarded-for"), ["192.168.1.50"])
        self.assertEqual(req.headers["accept"], "text/html")
        self.assertNotIn("6.6.6.6", repr(req.headers.raw))

    def test_broker_is_not_reachable_through_an_app_port(self):
        async def hub(scope, receive, send):
            await proxy._plain(send, 200, b"hub")

        dispatcher = proxy.PortDispatcher(hub)
        for path in ("/_broker", "/_broker/rpc/state.get", "/_broker/rpc/../rpc/x"):
            self.assertEqual(response(run(dispatcher, scope(self.port, path)))[0], 404, path)
        self.assertEqual(self.upstream.requests, [])


class FrameTests(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        make_install()

    def test_sandbox_never_allows_top_navigation(self):
        for url in ("/apps/cams/", "/apps/cams/_bare/x"):
            html = self.client.get(url).content.decode()
            sandbox = re.findall(r'sandbox="([^"]*)"', html)
            self.assertEqual(len(sandbox), 1, url)
            self.assertNotIn("allow-top-navigation", sandbox[0])
            self.assertNotIn("allow-modals", sandbox[0])

    def test_hub_session_cookie_is_http_only(self):
        # App JS runs on the hub's host (another port), so it shares the hub's cookie jar.
        request = RequestFactory().get("/")
        middleware = SessionMiddleware(lambda r: HttpResponse())
        middleware.process_request(request)
        login(request, User.objects.create_user("stephen", password="x"),
              backend="django.contrib.auth.backends.ModelBackend")
        response = middleware.process_response(request, HttpResponse())
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]["httponly"])


def frame_messages(messages, bare="0"):
    out = subprocess.run(["node", str(HARNESS), str(EMBED_HOST), bare], input=json.dumps(messages),
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def msg(data, origin=APP_ORIGIN, source="frame"):
    return {"origin": origin, "source": source, "data": data}


NOTHING = {"nav": [], "title": "", "style": "", "appended": [], "events": [], "posted": [], "error": None}


class PostMessageTests(TestCase):
    """Runs embed-host.js under node and spoofs messages at it."""

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")

    def test_only_the_app_frame_on_the_app_origin_is_heard(self):
        good = {"bdh": 1, "type": "title", "title": "hi"}
        results = frame_messages([
            msg(good),
            msg(good, origin="http://hub.local:8000"),
            msg(good, origin="http://hub.local:9101"),
            msg(good, origin="null"),
            msg(good, source="other"),
        ])
        self.assertEqual(results[0]["title"], "hi")
        self.assertEqual(results[1:], [NOTHING] * 4)

    def test_bad_schema_is_ignored(self):
        results = frame_messages([msg(d) for d in (
            '{"bdh":1,"type":"title","title":"x"}', None, [], {"type": "title", "title": "x"},
            {"bdh": "1", "type": "title", "title": "x"}, {"bdh": 1, "type": 5},
            {"bdh": 1, "type": "__proto__"}, {"bdh": 1, "type": "constructor"},
            {"bdh": 1, "type": "hasOwnProperty"}, {"bdh": 1, "type": "toString"},
        )])
        self.assertEqual(results, [NOTHING] * len(results))

    def test_navigate_stays_on_the_hub(self):
        nav = [msg({"bdh": 1, "type": "navigate", "path": p}) for p in (
            "/apps/cams/live?x=1", "/", "/settings/", "/admin/", "/accounts/logout/", "/display/settings/",
            "/apps/_nav/?name=admin:index", "/apps/cams/_raw/x.png", "/apps/cams/_bare/",
            "/apps/../admin/", "//evil.example/", "https://evil.example/", "javascript:alert(1)",
            "/\\evil.example", "/\tevil", "\\\\evil.example", "/_broker/rpc/state.get", "/_broker",
            "/%2e%2e/_broker/rpc/x", "/x/../_broker/rpc/x", None, 5)]
        nav += [msg({"bdh": 1, "type": "navigate", "name": n}) for n in (
            "javascript:alert(1)//", "admin:index", "logout", "display_settings", "toString", "__proto__")]
        nav.append(msg({"bdh": 1, "type": "navigate", "name": "broker:shell", "args": ["cams", {"x": 1}]}))
        results = frame_messages(nav)
        self.assertEqual(results[0]["nav"], ["/apps/cams/live?x=1"])
        self.assertEqual(results[1]["nav"], ["/"])
        for r in results[2:-1]:
            self.assertEqual(r["nav"], [], r)
        self.assertEqual(results[-1]["nav"], ["/apps/_nav/?name=broker%3Ashell&args=%5B%22cams%22%5D"])

    def test_app_may_only_open_its_own_settings(self):
        results = frame_messages([msg({"bdh": 1, "type": "navigate", "name": "hub_settings_app", "args": a})
                                  for a in (["cams"], ["wifi"], [], ["cams", "x"], None)])
        self.assertEqual(results[0]["nav"], ["/apps/_nav/?name=hub_settings_app&args=%5B%22cams%22%5D"])
        for r in results[1:]:
            self.assertEqual(r["nav"], [], r)

    def test_theme_cannot_inject_css(self):
        results = frame_messages([msg({"bdh": 1, "type": "theme", "background": c}) for c in (
            "#123456", "red", "#000}body{display:none", "rgb(1,2,3)}*{x:y", "url(http://evil/)", "rgba(0,0,0,0)")])
        self.assertIn("#123456", results[0]["style"])
        self.assertEqual([r["style"] for r in results[1:]], [""] * 5)

    def test_alert_commands_reach_the_overlay_only_from_a_popup(self):
        cmds = [msg({"bdh": 1, "type": "alert", "cmd": c})
                for c in ("hold", "release", "hangup", "close", "toString", None)]
        results = frame_messages(cmds + [msg({"bdh": 1, "type": "alert", "cmd": "hold"}, origin="null")], bare="1")
        self.assertEqual([r["posted"] for r in results[:3]],
                         [[{"data": {"bdhAlert": c}, "origin": "http://hub.local:8000"}]
                          for c in ("hold", "release", "hangup")])
        self.assertEqual(results[3:], [NOTHING] * 4)
        # A normal app page (not in the alert overlay) can't touch alerts.
        self.assertEqual(frame_messages(cmds[:1]), [NOTHING])
