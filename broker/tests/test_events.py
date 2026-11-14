import json
from unittest import mock

from bdh_sdk import events as bus
from django.test import TestCase

from broker import events_bridge, remote

from .base import make_install, reset_broker


class EventsBridgeTests(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        patcher = mock.patch("broker.events_bridge._submit")
        self.submit = patcher.start()
        self.addCleanup(patcher.stop)

    def delivered(self):
        bus.flush()
        return [(app, event.topic) for (app, event), _ in self.submit.call_args_list]

    def test_matching_topics_forwarded(self):
        events_bridge.set_patterns("cams", ["ha/state/sensor/#"])
        self.assertIn("ha/state/sensor/#", bus.patterns())
        bus.publish("ha/state/sensor/temp", {"v": 1})
        bus.publish("ha/state/light/kitchen", {"v": 2})
        self.assertEqual(self.delivered(), [("cams", "ha/state/sensor/temp")])

    def test_overlapping_patterns_send_once(self):
        events_bridge.set_patterns("cams", ["ha/#", "ha/state/sensor/#", "ha/state/+/temp"])
        bus.publish("ha/state/sensor/temp", 1)
        self.assertEqual(self.delivered(), [("cams", "ha/state/sensor/temp")])

    def test_replace_and_drop(self):
        events_bridge.set_patterns("cams", ["a/#", "b/#"])
        events_bridge.set_patterns("cams", ["b/#"])
        self.assertEqual(events_bridge.patterns("cams"), ["b/#"])
        self.assertNotIn("a/#", bus.patterns())
        bus.publish("a/x")
        bus.publish("b/x")
        self.assertEqual(self.delivered(), [("cams", "b/x")])
        events_bridge.drop("cams")
        self.assertEqual(events_bridge.patterns("cams"), [])
        self.assertNotIn("b/#", bus.patterns())

    def test_no_echo_to_publisher(self):
        events_bridge.set_patterns("cams", ["cams/#"])
        events_bridge.set_patterns("other", ["cams/#"])
        events_bridge.publish("cams", "cams/motion", {"zone": 1})
        self.assertEqual(self.delivered(), [("other", "cams/motion")])
        bus.publish("cams/motion", {"zone": 1})  # someone else, same content
        self.assertIn(("cams", "cams/motion"), self.delivered())

    def test_disable_drops_subscriptions(self):
        install = make_install()
        events_bridge.set_patterns("cams", ["ha/#"])
        install.enabled = False
        install.save()
        self.assertEqual(events_bridge.patterns("cams"), [])

    def test_delete_drops_subscriptions(self):
        install = make_install()
        events_bridge.set_patterns("cams", ["ha/#"])
        install.delete()
        self.assertEqual(events_bridge.patterns("cams"), [])


class SendTests(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        # _send runs on a pool thread; here it would close the test's connection.
        patcher = mock.patch("django.db.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_send_signs_post(self):
        install = make_install()
        remote.remote_specs()
        events_bridge._pending = 1  # as _submit leaves it
        with mock.patch("broker.remote.signed_request") as req:
            req.return_value.status_code = 200
            events_bridge._send("cams", {"topic": "t", "data": 1, "time": 2.0})
        args = req.call_args
        self.assertEqual(args.args[1:4], ("POST", "/_bdh/events", {"topic": "t", "data": 1, "time": 2.0}))
        self.assertEqual(args.args[0].app_id, install.app_id)
        self.assertEqual(events_bridge._pending, 0)

    def test_send_failure_dropped(self):
        make_install()
        remote.remote_specs()
        events_bridge._pending = 1
        with mock.patch("broker.remote.signed_request", side_effect=OSError("down")):
            events_bridge._send("cams", {"topic": "t", "data": 1, "time": 2.0})
        self.assertEqual(events_bridge._pending, 0)

    def test_non_json_event_not_sent(self):
        with mock.patch("broker.events_bridge._executor") as ex:
            events_bridge._submit("cams", bus.Event("t", object()))
            ex.assert_not_called()
            events_bridge._submit("cams", bus.Event("t", {"a": 1}))
            payload = ex.return_value.submit.call_args.args[2]
        self.assertEqual(json.loads(json.dumps(payload))["data"], {"a": 1})
        events_bridge._pending = 0
