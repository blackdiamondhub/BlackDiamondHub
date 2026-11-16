import json
from unittest import mock

from bdh_sdk import events as bus
from django.test import TestCase

from broker import events_bridge, remote
from broker.models import AppInstall, AppSubscription

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


class PersistenceTests(TestCase):
    """The hub restarting (``reset``: memory gone, database kept) mustn't lose
    what container apps subscribed to, since they won't ask again."""

    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        patcher = mock.patch("broker.events_bridge._submit")
        self.submit = patcher.start()
        self.addCleanup(patcher.stop)
        self.install = make_install()

    def restart(self):
        events_bridge.reset()
        remote.installs_changed()

    def stored(self, app_id="cams"):
        row = AppSubscription.objects.filter(app_id=app_id).first()
        return row.payload if row else None

    def test_restored_after_restart(self):
        events_bridge.subscribe("cams", ["cams/#", "ha/state/sensor/#"])
        self.restart()
        self.assertEqual(events_bridge.patterns("cams"), [])
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), ["cams/#", "ha/state/sensor/#"])
        bus.publish("ha/state/sensor/temp", 1)
        bus.flush()
        self.assertEqual([(a, e.topic) for (a, e), _ in self.submit.call_args_list], [("cams", "ha/state/sensor/temp")])

    def test_latest_subscribe_replaces_stored(self):
        events_bridge.subscribe("cams", ["cams/a"])
        events_bridge.subscribe("cams", ["cams/b"])
        self.assertEqual(self.stored(), ["cams/b"])
        self.assertEqual(AppSubscription.objects.count(), 1)

    def test_empty_set_deletes_row(self):
        events_bridge.subscribe("cams", ["cams/#"])
        events_bridge.subscribe("cams", [])
        self.assertIsNone(self.stored())
        self.restart()
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), [])

    def test_subscribe_before_restore_wins(self):
        events_bridge.subscribe("cams", ["cams/old"])
        self.restart()
        events_bridge.subscribe("cams", ["cams/new"])  # the app got in first
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), ["cams/new"])

    def test_revoked_grant_dropped(self):
        events_bridge.subscribe("cams", ["cams/#", "ha/state/sensor/#"])
        # An upgrade in another process removes the permission; no signal reaches us.
        AppInstall.objects.filter(pk=self.install.pk).update(granted_permissions=[])
        remote.installs_changed()
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), ["cams/#"])
        # Stored as asked, so granting it again brings it back.
        self.assertEqual(self.stored(), ["cams/#", "ha/state/sensor/#"])
        AppInstall.objects.filter(pk=self.install.pk).update(granted_permissions=self.install.requested_permissions)
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), ["cams/#", "ha/state/sensor/#"])

    def test_disabled_not_restored_until_enabled(self):
        events_bridge.subscribe("cams", ["cams/#"])
        self.install.enabled = False
        self.install.save()
        self.assertEqual(events_bridge.patterns("cams"), [])
        self.restart()
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), [])
        self.install.enabled = True
        self.install.save()
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), ["cams/#"])

    def test_disabled_elsewhere_dropped_by_reconcile(self):
        events_bridge.subscribe("cams", ["cams/#"])
        AppInstall.objects.filter(pk=self.install.pk).update(enabled=False)
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), [])

    def test_uninstall_forgets(self):
        events_bridge.subscribe("cams", ["cams/#"])
        self.install.delete()
        self.assertIsNone(self.stored())
        self.assertEqual(events_bridge.patterns("cams"), [])

    def test_rows_without_install_cleaned_up(self):
        AppSubscription.objects.create(app_id="ghost", kind=AppSubscription.EVENTS, payload=["ghost/#"])
        events_bridge.set_patterns("ghost", ["ghost/#"])
        events_bridge.reconcile()
        self.assertIsNone(self.stored("ghost"))
        self.assertEqual(events_bridge.patterns("ghost"), [])

    def test_bad_stored_patterns_skipped(self):
        AppSubscription.objects.create(app_id="cams", kind=AppSubscription.EVENTS,
                                       payload=["cams/#", "a/#/b", 7, "#"])
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), ["cams/#"])
        AppSubscription.objects.filter(app_id="cams").update(payload={"not": "a list"})
        events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("cams"), [])

    def test_one_bad_app_does_not_stop_others(self):
        make_install(app_id="other")
        events_bridge.subscribe("cams", ["cams/#"])
        events_bridge.subscribe("other", ["other/#"])
        self.restart()
        real = events_bridge.sync

        def sync(app_id):
            if app_id == "cams":
                raise RuntimeError("boom")
            real(app_id)

        with mock.patch("broker.events_bridge.sync", side_effect=sync), self.assertLogs("broker.events_bridge"):
            events_bridge.reconcile()
        self.assertEqual(events_bridge.patterns("other"), ["other/#"])

    def test_loop_retries_until_database_is_up(self):
        calls = []

        def reconcile():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("database not up")
            if len(calls) == 3:
                raise SystemExit  # stop the loop

        waits = []
        with mock.patch("broker.events_bridge.reconcile", side_effect=reconcile), \
                mock.patch("broker.events_bridge.time.sleep", side_effect=waits.append), \
                mock.patch("django.db.close_old_connections"), self.assertLogs("broker.events_bridge"), \
                self.assertRaises(SystemExit):
            events_bridge._loop(60.0)
        self.assertEqual(waits, [2.0, 10.0, 60.0])


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
