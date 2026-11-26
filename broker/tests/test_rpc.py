import base64
import json
import os
from unittest import mock

from bdh_sdk import ha
from django.test import TestCase, override_settings

from broker import grants as g
from broker.models import AppInstall, AuditEntry
from broker.ratelimit import limiter
from broker.tokens import authenticate, token_for

from .base import auth, make_install, reset_broker


class RpcTestCase(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        self.app = make_install()

    def call(self, op, payload=None, install=None, token=None, port="8099"):
        headers = {"HTTP_AUTHORIZATION": token if token is not None else auth(install or self.app)}
        return self.client.post(f"/_broker/rpc/{op}", json.dumps(payload or {}),
                                content_type="application/json", SERVER_PORT=port, **headers)

    def ok(self, op, payload=None, **kw):
        resp = self.call(op, payload, **kw)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["ok"])
        return body["result"]

    def refused(self, op, payload=None, code="forbidden", status=403, **kw):
        resp = self.call(op, payload, **kw)
        self.assertEqual(resp.status_code, status, resp.content)
        self.assertEqual(resp.json()["error"]["code"], code)
        return resp


class TokenTests(RpcTestCase):
    def test_good_token(self):
        self.assertEqual(self.ok("state.keys", {"app": "cams"}), [])
        self.assertEqual(authenticate(token_for(self.app)), self.app)

    def test_bad_and_missing_tokens(self):
        self.refused("state.keys", {"app": "cams"}, token="Bearer nope")
        self.refused("state.keys", {"app": "cams"}, token="")
        self.refused("state.keys", {"app": "cams"}, token=token_for(self.app))  # no "Bearer"
        self.assertTrue(AuditEntry.objects.filter(app_id="?", allowed=False).exists())

    def test_disabled_app(self):
        self.app.enabled = False
        self.app.save()
        self.refused("state.keys", {"app": "cams"})

    def test_a_call_marks_the_app_running(self):
        from broker import lifecycle

        self.addCleanup(lifecycle.reset)
        with mock.patch.object(lifecycle, "managed", return_value=True):
            self.refused("state.keys", {"app": "cams"}, token="Bearer nope")
            self.assertNotIn("cams", lifecycle._running_until)
            self.ok("state.keys", {"app": "cams"})
        self.assertIn("cams", lifecycle._running_until)

    def test_rotated_generation_invalidates_old_token(self):
        old = auth(self.app)
        self.app.generation += 1
        self.app.save()
        self.refused("state.keys", {"app": "cams"}, token=old)
        self.ok("state.keys", {"app": "cams"})

    def test_other_apps_token_is_that_app(self):
        other = make_install("weather", upstream="http://127.0.0.1:9202")
        self.refused("state.keys", {"app": "cams"}, install=other)
        self.assertEqual(self.ok("state.keys", {"app": "weather"}, install=other), [])

    def test_get_and_unknown_op_and_bad_body(self):
        resp = self.client.get("/_broker/rpc/state.keys", SERVER_PORT="8099", HTTP_AUTHORIZATION=auth(self.app))
        self.assertEqual(resp.status_code, 405)
        self.refused("nope.nothing", code="unknown_op", status=404)
        resp = self.client.post("/_broker/rpc/state.keys", "[1]", content_type="application/json",
                                SERVER_PORT="8099", HTTP_AUTHORIZATION=auth(self.app))
        self.assertEqual(resp.status_code, 400)

    def test_body_cap(self):
        big = {"app": "cams", "key": "k", "value": "x" * (300 * 1024)}
        self.refused("state.set", big, code="too_large", status=413)


class OwnDataTests(RpcTestCase):
    def test_state_round_trip(self):
        self.ok("state.set", {"app": "cams", "key": "k", "value": {"a": 1}})
        self.assertEqual(self.ok("state.get", {"app": "cams", "key": "k"}), {"found": True, "value": {"a": 1}})
        self.assertEqual(self.ok("state.keys", {"app": "cams"}), ["k"])
        self.ok("state.delete", {"app": "cams", "key": "k"})
        self.assertEqual(self.ok("state.get", {"app": "cams", "key": "k"}), {"found": False, "value": None})
        self.assertTrue(AuditEntry.objects.filter(app_id="cams", op="state.set", allowed=True).exists())

    def test_other_apps_data_is_forbidden(self):
        for op, payload in [("state.get", {"app": "weather", "key": "k"}),
                            ("state.set", {"app": "weather", "key": "k", "value": 1}),
                            ("state.delete", {"app": "weather", "key": "k"}),
                            ("state.keys", {"app": "weather"}),
                            ("settings.get", {"app": "weather", "key": "k"}),
                            ("alerts.push", {"app": "weather", "key": "k", "title": "t"}),
                            ("alerts.dismiss", {"app": "weather", "key": "k"})]:
            with self.subTest(op=op):
                self.refused(op, payload)
        self.assertTrue(AuditEntry.objects.filter(app_id="cams", op="state.get", allowed=False).exists())

    def test_settings_get(self):
        with mock.patch("hub_settings.store.lookup", return_value=3) as lookup:
            self.assertEqual(self.ok("settings.get", {"app": "cams", "key": "grid"}), {"found": True, "value": 3})
        lookup.assert_called_once_with("cams", "grid")
        with mock.patch("hub_settings.store.lookup", side_effect=KeyError("grid")):
            self.assertEqual(self.ok("settings.get", {"app": "cams", "key": "grid"}),
                             {"found": False, "value": None})

    def test_settings_get_falls_back_to_site_config(self):
        site = {"cams": {"grid": {"a": 1}}, "weather": {"grid": 4}}
        with mock.patch("hub_settings.store.lookup", side_effect=KeyError("grid")), \
                override_settings(HUB_SITE_APPS=site):
            self.assertEqual(self.ok("settings.get", {"app": "cams", "key": "grid"}),
                             {"found": True, "value": {"a": 1}})
            self.assertEqual(self.ok("settings.get", {"app": "cams", "key": "other"}),
                             {"found": False, "value": None})
            # Never another app's table, and never the hub's environment.
            self.refused("settings.get", {"app": "weather", "key": "grid"})
            with mock.patch.dict(os.environ, {"BDH_CAMS_SECRET": "x"}):
                self.assertEqual(self.ok("settings.get", {"app": "cams", "key": "secret"}),
                                 {"found": False, "value": None})

    def test_any_app_reads_public_hub_settings(self):
        with mock.patch("hub_settings.store.lookup", return_value="imperial") as lookup:
            self.assertEqual(self.ok("settings.get", {"app": "hub", "key": "units"}),
                             {"found": True, "value": "imperial"})
            self.ok("settings.get", {"app": "hub", "key": "location"})
        self.assertEqual([c.args for c in lookup.call_args_list], [("hub", "units"), ("hub", "location")])
        self.refused("settings.get", {"app": "hub", "key": "quick"})
        self.refused("settings.get", {"app": "hub", "key": "home_layout"})


class AlertTests(RpcTestCase):
    def test_push_needs_grant(self):
        self.app.granted_permissions = []
        self.app.save()
        self.refused("alerts.push", {"app": "cams", "key": "door", "title": "Door"})

    def test_push_rewrites_paths_onto_hub(self):
        with mock.patch("bdh_sdk.alerts.push") as push:
            self.ok("alerts.push", {"app": "cams", "key": "door", "title": "Door",
                                    "embed_url": "/cameras/popup/front", "sound": "/static/ding.mp3"})
        alert = push.call_args.args[0]
        self.assertEqual(alert.app, "cams")
        self.assertEqual(alert.embed_url, "/apps/cams/_bare/cameras/popup/front")
        self.assertEqual(alert.sound, "/apps/cams/_raw/static/ding.mp3")

    def test_push_keeps_admins_only(self):
        with mock.patch("bdh_sdk.alerts.push") as push:
            self.ok("alerts.push", {"app": "cams", "key": "door", "title": "Door",
                                    "embed_url": "/cameras/popup/front", "admins_only": True})
        self.assertTrue(push.call_args.args[0].admins_only)
        self.refused("alerts.push", {"app": "cams", "key": "door", "title": "Door",
                                     "embed_url": "/cameras/popup/front", "admins_only": "yes"},
                     code="invalid", status=400)

    def test_push_invalid(self):
        self.refused("alerts.push", {"app": "cams", "key": "", "title": "Door"}, code="invalid", status=400)

    def test_dismiss(self):
        with mock.patch("bdh_sdk.alerts.dismiss") as dismiss:
            self.ok("alerts.dismiss", {"app": "cams", "key": "door"})
        dismiss.assert_called_once_with("cams", "door")


class HomeAssistantTests(RpcTestCase):
    def test_read_glob(self):
        with mock.patch("bdh_sdk.ha.get_entity_state", return_value={"state": "3"}) as get:
            self.assertEqual(self.ok("ha.get_entity_state", {"entity_id": "sensor.temp"}), {"state": "3"})
            self.refused("ha.get_entity_state", {"entity_id": "lock.front_door"})
        get.assert_called_once_with("sensor.temp", instance=None)
        with mock.patch("bdh_sdk.ha.get_entity_states", return_value={}) as many:
            self.refused("ha.get_entity_states", {"entity_ids": ["sensor.a", "lock.b"]})
            self.ok("ha.get_entity_states", {"entity_ids": ["sensor.a"]})
        many.assert_called_once_with(["sensor.a"], instance=None)

    def test_instance_passthrough(self):
        from hub_settings import ha as instances
        from hub_settings.models import HAInstance

        self.addCleanup(instances.clear)
        HAInstance.objects.create(id="home", name="Home", url="http://a:8123", is_default=True)
        HAInstance.objects.create(id="cabin", name="Cabin", site="Sun Peaks", url="http://b:8123", order=1)
        self.assertEqual(self.ok("ha.instances"), [
            {"id": "home", "name": "Home", "site": "", "default": True},
            {"id": "cabin", "name": "Cabin", "site": "Sun Peaks", "default": False},
        ])
        with mock.patch("bdh_sdk.ha.get_entity_state", return_value={"state": "1"}) as get:
            self.ok("ha.get_entity_state", {"entity_id": "sensor.a", "instance": "cabin"})
            self.refused("ha.get_entity_state", {"entity_id": "sensor.a", "instance": "nope"},
                         code="invalid", status=400)
            self.refused("ha.get_entity_state", {"entity_id": "sensor.a", "instance": 3},
                         code="invalid", status=400)
        get.assert_called_once_with("sensor.a", instance="cabin")

    def test_instances_needs_a_read_grant(self):
        self.app.granted_permissions = ["alerts"]
        self.app.save()
        self.refused("ha.instances")

    def test_all_states_filtered(self):
        states = [{"entity_id": "sensor.a"}, {"entity_id": "lock.b"}, {"entity_id": "sensor.c"}]
        with mock.patch("bdh_sdk.ha.get_all_states", return_value=states):
            got = self.ok("ha.get_all_states")
        self.assertEqual([s["entity_id"] for s in got], ["sensor.a", "sensor.c"])
        self.app.granted_permissions = ["alerts"]
        self.app.save()
        self.refused("ha.get_all_states")

    def test_history_is_json(self):
        from datetime import datetime, timezone

        when = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        with mock.patch("bdh_sdk.ha.get_history", return_value={"sensor.a": [(when, 1.5)]}) as hist:
            got = self.ok("ha.get_history", {"entity_ids": ["sensor.a"], "start": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(got, {"sensor.a": [["2026-01-02T03:04:00+00:00", 1.5]]})
        self.assertIsNone(hist.call_args.args[2])
        self.refused("ha.get_history", {"entity_ids": ["sensor.a"], "start": "yesterday"},
                     code="invalid", status=400)

    def test_call_service_matching(self):
        with mock.patch("bdh_sdk.ha.call_service", return_value=(True, None)) as call:
            self.assertEqual(self.ok("ha.call_service", {"domain": "light", "service": "turn_on",
                                                         "entity_id": "light.x"}), [True, None])
            self.refused("ha.call_service", {"domain": "light", "service": "turn_off"})
            self.refused("ha.call_service", {"domain": "lock", "service": "unlock"})
        call.assert_called_once_with("light", "turn_on", "light.x", None, instance=None)
        self.assertTrue(AuditEntry.objects.filter(op="ha.call_service", allowed=True).exists())
        self.assertTrue(AuditEntry.objects.filter(op="ha.call_service", allowed=False).exists())

    def test_ha_error(self):
        with mock.patch("bdh_sdk.ha.get_entity_state", side_effect=ha.HAError("down")):
            self.refused("ha.get_entity_state", {"entity_id": "sensor.a"}, code="ha_error", status=500)

    def test_media_ops_need_media(self):
        self.refused("ha.upload_media", {"folder": "x", "filename": "a.jpg",
                                         "data": base64.b64encode(b"x").decode(), "content_type": "image/jpeg"})
        self.refused("ha.get_static", {"path": "/local/x.png"})
        self.refused("ha.get_tts", {"engine_id": "tts.x", "message": "hi"})
        self.app.granted_permissions = self.app.requested_permissions = ["media"]
        self.app.save()
        with mock.patch("bdh_sdk.ha.get_static", return_value=b"png") as static:
            self.assertEqual(self.ok("ha.get_static", {"path": "/local/x.png"}), base64.b64encode(b"png").decode())
            self.refused("ha.get_static", {"path": "http://evil/"}, code="invalid", status=400)
        static.assert_called_once()
        with mock.patch("bdh_sdk.ha.upload_media", return_value="media-source://x") as up:
            self.ok("ha.upload_media", {"folder": "cams", "filename": "a.jpg",
                                        "data": base64.b64encode(b"x").decode(), "content_type": "image/jpeg"})
            self.refused("ha.upload_media", {"folder": "../etc", "filename": "a", "data": "eA==",
                                             "content_type": "x"}, code="invalid", status=400)
        self.assertEqual(up.call_args.args[2], b"x")

    def test_ws_commands_need_ha_ws(self):
        self.refused("ha.ws_commands", {"messages": [{"type": "get_config"}]})
        self.app.granted_permissions = self.app.requested_permissions = [g.HA_WS]
        self.app.save()
        with mock.patch("bdh_sdk.ha.ws_commands", return_value=[{"ok": 1}]):
            self.assertEqual(self.ok("ha.ws_commands", {"messages": [{"type": "get_config"}]}), [{"ok": 1}])

    def test_registry_needs_a_read_grant(self):
        self.app.granted_permissions = self.app.requested_permissions = [g.HA_WS]
        self.app.save()
        with mock.patch("bdh_sdk.ha.get_registry") as reg:
            self.refused("ha.registry")
        reg.assert_not_called()

    REGISTRY = {
        "entities": [
            {"entity_id": "sensor.fp_temp", "device_id": "d1", "platform": "napoleon"},
            {"entity_id": "lock.front", "device_id": "d2", "platform": "napoleon"},
            {"entity_id": "sensor.hue", "device_id": "d3", "platform": "hue"},
            {"entity_id": "sensor.loose", "device_id": None, "platform": "napoleon"},
        ],
        "devices": [{"id": "d1", "name": "FP"}, {"id": "d2", "name": "Lock"},
                    {"id": "d3", "name": "Bridge"}, {"id": "d4", "name": "Unused"}],
        "unit_system": {"temperature": "°F"},
    }

    def test_registry_filtered_by_read_globs(self):
        with mock.patch("bdh_sdk.ha.get_registry", return_value=self.REGISTRY) as reg:
            got = self.ok("ha.registry")
        reg.assert_called_once_with(platform=None, instance=None)
        self.assertEqual([e["entity_id"] for e in got["entities"]],
                         ["sensor.fp_temp", "sensor.hue", "sensor.loose"])
        self.assertEqual([d["id"] for d in got["devices"]], ["d1", "d3"])
        self.assertEqual(got["unit_system"], {"temperature": "°F"})
        self.assertEqual(got["areas"], [])

    def test_registry_keeps_areas_of_readable_entities_and_devices(self):
        reg = {
            "entities": [{"entity_id": "sensor.fp_temp", "device_id": "d1", "area_id": "den"},
                         {"entity_id": "lock.front", "device_id": "d2", "area_id": "hall"}],
            "devices": [{"id": "d1", "area_id": "lounge"}, {"id": "d2", "area_id": "porch"}],
            "areas": [{"area_id": a, "name": a.title()} for a in ("den", "hall", "lounge", "porch")],
        }
        with mock.patch("bdh_sdk.ha.get_registry", return_value=reg):
            got = self.ok("ha.registry")
        self.assertEqual([a["area_id"] for a in got["areas"]], ["den", "lounge"])

    def test_registry_platform_filter(self):
        with mock.patch("bdh_sdk.ha.get_registry", return_value=self.REGISTRY) as reg:
            got = self.ok("ha.registry", {"platform": "napoleon"})
            self.refused("ha.registry", {"platform": 3}, code="invalid", status=400)
        reg.assert_called_once_with(platform="napoleon", instance=None)
        self.assertEqual([e["entity_id"] for e in got["entities"]], ["sensor.fp_temp", "sensor.loose"])
        self.assertEqual(got["devices"], [{"id": "d1", "name": "FP"}])

    def test_registry_ha_error(self):
        with mock.patch("bdh_sdk.ha.get_registry", side_effect=ha.HAError("down")):
            self.refused("ha.registry", code="ha_error", status=500)

    def test_grant_must_also_be_requested(self):
        self.app.granted_permissions = ["ha.read:*"]
        self.app.save()
        self.refused("ha.get_entity_state", {"entity_id": "sensor.a"})


class SpeakerTests(RpcTestCase):
    def test_speakers_need_grant(self):
        self.refused("speakers.list")
        self.refused("speakers.announce", {"message": "hi", "speakers": ["a"]})
        self.app.granted_permissions = self.app.requested_permissions = ["speakers"]
        self.app.save()
        with mock.patch("bdh_sdk.speakers.list_speakers", return_value=[("a", "Kitchen")]):
            self.assertEqual(self.ok("speakers.list"), [["a", "Kitchen"]])
        with mock.patch("bdh_sdk.speakers.announce", return_value=[]) as announce:
            self.assertEqual(self.ok("speakers.announce", {"message": "hi", "speakers": ["a"], "volume": 0.5}), [])
        self.assertEqual(announce.call_args.args[:3], ("hi", ["a"], 0.5))


class DisplaysStatusTests(RpcTestCase):
    def test_needs_grant_and_leaks_no_page_details(self):
        from datetime import timedelta

        from django.utils import timezone

        from display.models import Heartbeat

        Heartbeat.objects.create(ip="192.168.1.99", last_seen=timezone.now() - timedelta(seconds=90),
                                 path="/secret/page?token=x", user_agent="Mozilla/5.0 X11")
        self.refused("displays.status")
        self.app.granted_permissions = self.app.requested_permissions = [g.DISPLAYS_READ]
        self.app.save()
        result = self.ok("displays.status")
        self.assertEqual(set(result), {"ready_since", "now", "clients"})
        [client] = result["clients"]
        self.assertEqual(set(client), {"ip", "last_seen", "age_seconds"})
        self.assertEqual(client["ip"], "192.168.1.99")
        self.assertGreaterEqual(client["age_seconds"], 90)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("Mozilla", json.dumps(result))
        self.assertLessEqual(result["ready_since"], result["now"])


class StaysCurrentTests(RpcTestCase):
    def test_needs_grant_and_returns_stay_with_identity_key(self):
        from datetime import datetime, timedelta, timezone

        from django.test import RequestFactory

        from hub.context_processors import stay_key

        mt = timezone(timedelta(hours=-7))
        stay = {"guest": "The Smiths", "arrival": datetime(2027, 1, 4, 16, tzinfo=mt),
                "checkout": datetime(2027, 1, 8, 11, tzinfo=mt), "owner": False}
        with mock.patch("hub.status.current_stay", return_value=stay) as cur:
            self.refused("stays.current")
            cur.assert_not_called()
            self.app.granted_permissions = self.app.requested_permissions = [g.STAYS_READ]
            self.app.save()
            result = self.ok("stays.current")
            page_key = stay_key(RequestFactory().get("/"))["hub_stay_key"]()
        self.assertEqual(result, {"stay": {
            "guest": "The Smiths", "arrival": "2027-01-04T16:00:00-07:00",
            "checkout": "2027-01-08T11:00:00-07:00", "owner": False, "key": page_key}})
        self.assertEqual(len(page_key), 12)

    def test_no_stay(self):
        self.app.granted_permissions = self.app.requested_permissions = [g.STAYS_READ]
        self.app.save()
        with mock.patch("hub.status.current_stay", return_value=None):
            self.assertEqual(self.ok("stays.current"), {"stay": None})
        owners = {"guest": "Owners", "arrival": None, "checkout": None, "owner": True}
        with mock.patch("hub.status.current_stay", return_value=owners):
            stay = self.ok("stays.current")["stay"]
        self.assertEqual((stay["arrival"], stay["checkout"], stay["owner"]), (None, None, True))


class EventOpTests(RpcTestCase):
    def test_publish_own_namespace_and_grants(self):
        with mock.patch("broker.events_bridge.publish") as pub:
            self.ok("events.publish", {"topic": "cams/motion/front", "data": {"x": 1}})
            self.refused("events.publish", {"topic": "ha/state/lock/front"})
            self.refused("events.publish", {"topic": "weather/x"})
        pub.assert_called_once_with("cams", "cams/motion/front", {"x": 1})

    def test_subscribe_checks_cover(self):
        with mock.patch("broker.events_bridge.set_patterns") as setp:
            self.ok("events.subscribe", {"patterns": ["ha/state/sensor/+", "cams/#"]})
            self.refused("events.subscribe", {"patterns": ["ha/state/#"]})
            self.refused("events.subscribe", {"patterns": ["#"]})
            self.refused("events.subscribe", {"patterns": ["a/#/b"]}, code="invalid", status=400)
        setp.assert_called_once_with("cams", ["cams/#", "ha/state/sensor/+"])

    def test_subscribe_is_stored(self):
        from broker.models import AppSubscription

        with mock.patch("broker.events_bridge.set_patterns"):
            self.ok("events.subscribe", {"patterns": ["cams/#"]})
            self.refused("events.subscribe", {"patterns": ["#"]})
        self.assertEqual(AppSubscription.objects.get(app_id="cams").payload, ["cams/#"])


class RateLimitTests(RpcTestCase):
    def test_burst_then_429(self):
        with mock.patch("broker.ratelimit.time.monotonic", return_value=1000.0):
            statuses = [self.call("state.keys", {"app": "cams"}).status_code for _ in range(65)]
        self.assertEqual(statuses[:60], [200] * 60)
        self.assertIn(429, statuses[60:])
        self.assertTrue(AuditEntry.objects.filter(op="state.keys", detail="rate limited").exists())

    def test_slow_ops_and_per_app(self):
        self.assertEqual(sum(limiter.allow("cams", "speakers.announce", now=100.0) for _ in range(10)), 3)
        self.assertTrue(limiter.allow("other", "speakers.announce", now=100.0))
        self.assertTrue(limiter.allow("cams", "speakers.announce", now=106.0))


class AuditTests(TestCase):
    def test_ring_buffer(self):
        for i in range(120):
            AuditEntry.record("cams", "state.set", True, str(i))
        AuditEntry.trim(keep=50)
        details = list(AuditEntry.objects.order_by("id").values_list("detail", flat=True))
        self.assertEqual(details, [str(i) for i in range(70, 120)])


class GrantTests(TestCase):
    def test_grammar(self):
        grants = {"ha.read:sensor.*", "ha.call:light.*", "events.subscribe:ha/state/+/kitchen"}
        self.assertTrue(g.can_read(grants, "sensor.x"))
        self.assertFalse(g.can_read(grants, "switch.sensor"))
        self.assertTrue(g.can_call(grants, "light", "anything"))
        self.assertFalse(g.can_call(grants, "lights", "turn_on"))
        self.assertTrue(g.can_subscribe(grants, "cams", "ha/state/light/kitchen"))
        self.assertTrue(g.can_subscribe(grants, "cams", "ha/state/+/kitchen"))
        self.assertFalse(g.can_subscribe(grants, "cams", "ha/state/#"))
        self.assertTrue(g.can_subscribe(grants, "cams", "cams/+/x"))
        self.assertFalse(g.can_publish(grants, "cams", "cams/+"))

    def test_effective_grants(self):
        install = AppInstall(app_id="x", requested_permissions=["alerts", "speakers"],
                             granted_permissions=["alerts", "media"])
        self.assertEqual(install.grants, frozenset({"alerts"}))

    def test_sdk_fake_broker_mirrors_the_hub(self):
        """bdh_sdk.testing.FakeBroker.for_plugin must refuse what this broker refuses,
        so app tests catch an app relying on an op its manifest doesn't grant."""
        from bdh_sdk import grants as sdk_g
        from bdh_sdk.testing import PERMISSION_RULES

        from broker import rpc

        self.assertEqual(set(rpc.OPS), set(PERMISSION_RULES))
        grants = {"ha.read:sensor.*", "ha.read:light.kitchen", "ha.call:light.*",
                  "ha.call:lock.lock", "events.subscribe:ha/state/+/kitchen",
                  "events.publish:shared/#"}
        for eid in ("sensor.x", "switch.sensor", "light.kitchen", "light.den", "", None):
            self.assertEqual(sdk_g.can_read(grants, eid), g.can_read(grants, eid), eid)
        for domain, service in (("light", "turn_on"), ("lights", "turn_on"),
                                ("lock", "lock"), ("lock", "unlock")):
            self.assertEqual(sdk_g.can_call(grants, domain, service),
                             g.can_call(grants, domain, service), (domain, service))
        for pattern in ("ha/state/light/kitchen", "ha/state/#", "cams/+/x", "shared/a", "timers/x"):
            self.assertEqual(sdk_g.can_subscribe(grants, "cams", pattern),
                             g.can_subscribe(grants, "cams", pattern), pattern)
            self.assertEqual(sdk_g.can_publish(grants, "cams", pattern),
                             g.can_publish(grants, "cams", pattern), pattern)


class PortIsolationTests(RpcTestCase):
    def test_broker_only_on_broker_port(self):
        for port in ("80", "8000", "9100"):
            with self.subTest(port=port):
                self.assertEqual(self.call("state.keys", {"app": "cams"}, port=port).status_code, 404)
        self.assertEqual(self.client.get("/", SERVER_PORT="8099").status_code, 404)
        self.assertEqual(self.client.get("/_broker/rpc/", SERVER_PORT="8099").status_code, 404)
        self.assertEqual(self.client.get("/admin/", SERVER_PORT="8099").status_code, 404)
