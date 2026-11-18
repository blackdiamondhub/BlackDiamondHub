import asyncio
import json
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from bdh_sdk import events as bus
from django.test import override_settings
from django.urls import reverse

from broker import proxy, push

from .test_proxy import ProxyTestCase, response, run, scope


def reply(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body)


class Socket:
    """Drives one push websocket through the proxy, like a browser would."""

    def __init__(self, port, headers):
        self.inbox = asyncio.Queue()
        self.sent = []
        self.changed = asyncio.Event()
        self.inbox.put_nowait({"type": "websocket.connect"})
        sc = scope(port, push.WS_PATH, headers=headers, kind="websocket")
        self.task = asyncio.ensure_future(proxy.app_proxy(sc, self.inbox.get, self._send))

    async def _send(self, message):
        self.sent.append(message)
        self.changed.set()

    async def until(self, test, timeout=3):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not test():
            self.changed.clear()
            left = end - loop.time()
            if left <= 0:
                raise AssertionError(f"timed out; sent {self.sent}")
            try:
                await asyncio.wait_for(self.changed.wait(), left)
            except asyncio.TimeoutError:
                pass

    def frames(self):
        return [json.loads(m["text"]) for m in self.sent if m["type"] == "websocket.send"]

    def closed(self):
        return next((m.get("code") for m in self.sent if m["type"] == "websocket.close"), None)

    async def hang_up(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1001})
        await asyncio.wait_for(self.task, 3)


@override_settings(ALLOWED_HOSTS=["hub.local"])
class PushTests(ProxyTestCase):
    def setUp(self):
        super().setUp()
        push.reset()
        self.addCleanup(push.reset)
        self.origin = f"http://hub.local:{self.port}"
        self.calls = []
        self.answer = reply(200, {"patterns": ["cams/#", "other/#", "cams/bad/#/x"]})
        patcher = mock.patch.object(push.remote, "signed_request", side_effect=self._authorize)
        patcher.start()
        self.addCleanup(patcher.stop)
        ensure = mock.patch.object(proxy.lifecycle, "ensure_running")
        self.ensure = ensure.start()
        self.addCleanup(ensure.stop)

    def _authorize(self, install, method, path, payload=None, **kw):
        self.calls.append((install.app_id, method, path, kw))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def headers(self, origin=None, session=None):
        h = [("origin", origin or self.origin), ("user-agent", "TestBrowser")]
        if session:
            h.append(("cookie", f"sessionid={session}"))
        return h

    def test_info_answers_without_waking_the_app(self):
        status, headers, body = response(run(proxy.app_proxy, scope(self.port, push.INFO_PATH)))
        self.assertEqual((status, json.loads(body)), (200, {"proto": 1}))
        self.assertEqual(headers["cache-control"], ["no-store"])
        self.ensure.assert_not_called()
        self.assertEqual(self.upstream.requests, [])

    def test_info_needs_a_known_host_and_app(self):
        bad = scope(self.port, push.INFO_PATH, headers=[("host", "evil.example:9100")])
        self.assertEqual(response(run(proxy.app_proxy, bad))[0], 400)
        self.assertEqual(response(run(proxy.app_proxy, scope(self.port + 50, push.INFO_PATH)))[0], 404)

    def test_other_internal_paths_stay_refused(self):
        self.assertEqual(response(run(proxy.app_proxy, scope(self.port, push.WS_PATH)))[0], 403)
        sent = run(proxy.app_proxy, scope(self.port, push.INFO_PATH, kind="websocket",
                                          headers=[("origin", self.origin)]))
        self.assertEqual(sent[-1]["type"], "websocket.close")

    def test_relays_matching_topics_as_the_visitor(self):
        session = self.hub_cookie(is_staff=True)

        async def go():
            ws = Socket(self.port, self.headers(session=session))
            await ws.until(lambda: ws.frames())
            self.assertEqual(ws.frames()[0], {"t": "hello", "proto": 1, "patterns": ["cams/#"]})
            self.assertEqual(ws.sent[0], {"type": "websocket.accept"})
            bus.publish("other/x", 1)
            bus.publish("cams/protect/a/changed", 1)
            await ws.until(lambda: len(ws.frames()) > 1)
            self.assertEqual(ws.frames()[1], {"t": "inv", "topics": ["cams/protect/a/changed"]})
            self.assertEqual(push.stats()["connections"][0]["user"], "stephen")
            await ws.hang_up()

        async_to_sync(go)()
        app_id, method, path, kw = self.calls[0]
        self.assertEqual((app_id, method, path), ("cams", "GET", "/_bdh/push/authorize"))
        self.assertEqual(kw["identity"]["u"], "stephen")
        self.assertFalse(kw["wake"])
        self.ensure.assert_not_called()
        s = push.stats()
        self.assertEqual((s["connections"], s["subscriptions"]), ([], []))
        self.assertEqual(s["closed"][0]["code"], 1001)
        self.assertNotIn("cams/#", bus.patterns())

    def test_refusals(self):
        cases = [
            (reply(404), push.NO_PUSH),
            (reply(403), push.REFUSED),
            (reply(200, {"patterns": ["other/#"]}), push.REFUSED),
            (reply(500), push.APP_DOWN),
            (ConnectionError("down"), push.APP_DOWN),
        ]
        for answer, code in cases:
            self.answer = answer
            sent = run(proxy.app_proxy, scope(self.port, push.WS_PATH, headers=self.headers(), kind="websocket"))
            self.assertEqual(sent, [{"type": "websocket.close", "code": code}], answer)
        sent = run(proxy.app_proxy, scope(self.port, push.WS_PATH, kind="websocket",
                                          headers=self.headers(origin="http://evil.example")))
        self.assertEqual(sent, [{"type": "websocket.close", "code": push.REFUSED}])
        self.assertEqual(push.stats()["totals"]["refused"], 6)

    def test_caps_connections_per_address(self):
        async def go():
            socks = [Socket(self.port, self.headers()) for _ in range(push.MAX_PER_IP)]
            for ws in socks:
                await ws.until(lambda ws=ws: ws.frames())
            extra = Socket(self.port, self.headers())
            await extra.until(lambda: extra.closed())
            self.assertEqual(extra.closed(), push.BUSY)
            for ws in socks:
                await ws.hang_up()

        async_to_sync(go)()

    def test_overflow_asks_for_a_resync(self):
        async def go():
            ws = Socket(self.port, self.headers())
            await ws.until(lambda: ws.frames())
            conn = next(iter(push._conns.values()))
            for i in range(push.QUEUE + 1):
                conn.offer(f"cams/t{i}")
            await ws.until(lambda: len(ws.frames()) > 1)
            self.assertEqual(ws.frames()[1], {"t": "resync"})
            await ws.hang_up()

        async_to_sync(go)()

    def test_staff_can_disconnect_and_client_stats_are_kept(self):
        async def go():
            ws = Socket(self.port, self.headers())
            await ws.until(lambda: ws.frames())
            ws.inbox.put_nowait({"type": "websocket.receive",
                                 "text": json.dumps({"t": "stats", "s": {"refreshes": 3, "x" * 40: 1}})})
            await asyncio.sleep(0.05)
            conn_id = push.stats()["connections"][0]["id"]
            self.assertEqual(push.stats()["connections"][0]["client"]["refreshes"], 3)
            self.assertTrue(push.disconnect(conn_id))
            await ws.until(lambda: ws.closed())
            self.assertEqual(ws.closed(), push.KICKED)
            await asyncio.wait_for(ws.task, 3)

        async_to_sync(go)()
        self.assertEqual(push.stats()["codes"], {str(push.KICKED): 1})

    def test_stats_page_is_staff_only(self):
        url = reverse("hub_settings_push")
        self.client.defaults["HTTP_HOST"] = "hub.local"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.hub_cookie(is_staff=True)
        self.assertContains(self.client.get(url), "Push internals")
        self.assertEqual(self.client.get(url + "?live=1").json()["proto"], 1)
        self.assertEqual(self.client.post(url, {"action": "resync", "conn": "99"}).json(), {"ok": False})
        self.assertEqual(self.client.post(url, {"action": "nope", "conn": "1"}).status_code, 400)
