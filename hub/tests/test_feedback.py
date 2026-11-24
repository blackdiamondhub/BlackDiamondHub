import importlib
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, TransactionTestCase

from broker import remote
from hub import feedback
from hub_settings.state import DatabaseStateBackend

MARKERS = ('id="feedback-button"', 'id="feedback-modal"', 'class="hub-menu-fb"', 'id="inbox-link"')


class TopBarTests(TestCase):
    """The pill, menu items and modal show only when the feedback container app is installed."""

    def setUp(self):
        self.staff = User.objects.create_user("owner", password="pw", is_staff=True)

    def test_hidden_without_the_app(self):
        self.client.force_login(self.staff)
        page = self.client.get("/").content.decode()
        for marker in MARKERS:
            self.assertNotIn(marker, page)
        self.assertIn('id="user-menu"', page)
        self.assertIsNone(feedback.context(mock.Mock(user=self.staff))["hub_feedback"])

    def test_container_app_drives_the_top_bar(self):
        DatabaseStateBackend().set("feedback", "unread_count", 3)
        self.client.force_login(self.staff)
        with mock.patch.object(feedback, "container_enabled", return_value=True):
            page = self.client.get("/").content.decode()
        self.assertIn('id="feedback-button"', page)
        self.assertIn('action="/hub/api/feedback/submit/"', page)
        self.assertIn(f'href="{remote.page_url("feedback", "/feedback/")}" role="menuitem" id="inbox-link"', page)
        self.assertIn('title="Unread feedback">3<', page)

    def test_guests_see_the_pill_but_no_count(self):
        DatabaseStateBackend().set("feedback", "unread_count", 3)
        with mock.patch.object(feedback, "container_enabled", return_value=True):
            page = self.client.get("/").content.decode()
        self.assertIn('id="feedback-button"', page)
        self.assertNotIn("Unread feedback", page)

    def test_lookup_failure_hides_it(self):
        with mock.patch("broker.remote.spec_for", side_effect=RuntimeError("db down")):
            self.assertFalse(feedback.container_enabled())

@mock.patch.object(feedback, "container_enabled", return_value=True)
class ContainerSubmitTests(TestCase):
    def test_forwards_the_message_with_page_and_stay(self, _enabled):
        with mock.patch.object(remote, "post_form", return_value=mock.Mock(status_code=200)) as post, \
                mock.patch.object(feedback, "stay_label", return_value="the Smith stay"):
            resp = self.client.post("/hub/api/feedback/submit/",
                                    {"message": "  Great place ", "page_url": "http://x/"})
        self.assertEqual(resp.json(), {"success": True})
        app_id, path, data = post.call_args.args
        self.assertEqual((app_id, path), ("feedback", "/feedback/submit/"))
        self.assertEqual(data, {"message": "Great place", "page_url": "http://x/", "page_title": "Home",
                                "page_icon": "", "stay": "the Smith stay"})
        self.assertEqual(post.call_args.kwargs["identity"][feedback.SUBMIT_FLAG], 1)

    def test_visitors_cannot_set_page_details(self, _enabled):
        with mock.patch.object(remote, "post_form", return_value=mock.Mock(status_code=200)) as post:
            self.client.post("/hub/api/feedback/submit/",
                             {"message": "x", "page_url": "/nowhere/", "page_title": "Fake", "stay": "Fake"})
        data = post.call_args.args[2]
        self.assertEqual((data["page_title"], data["stay"]), ("/nowhere/", ""))

    def test_empty_message_is_rejected(self, _enabled):
        with mock.patch.object(remote, "post_form") as post:
            resp = self.client.post("/hub/api/feedback/submit/", {"message": "   "})
        self.assertEqual(resp.status_code, 400)
        post.assert_not_called()

    def test_app_failure_is_reported(self, _enabled):
        with mock.patch.object(remote, "post_form", side_effect=remote.RemoteError("down")):
            resp = self.client.post("/hub/api/feedback/submit/", {"message": "hi"})
        self.assertEqual((resp.status_code, resp.json()["success"]), (502, False))
        with mock.patch.object(remote, "post_form", return_value=mock.Mock(status_code=403)):
            resp = self.client.post("/hub/api/feedback/submit/", {"message": "hi"})
        self.assertEqual(resp.status_code, 502)


class ContainerOffTests(TestCase):
    def test_submit_is_404_without_the_app(self):
        with mock.patch.object(remote, "post_form") as post:
            resp = self.client.post("/hub/api/feedback/submit/", {"message": "hi"})
        self.assertEqual(resp.status_code, 404)
        post.assert_not_called()


class UnreadTests(TestCase):
    def test_only_staff_see_the_app_count(self):
        DatabaseStateBackend().set("feedback", "unread_count", 4)
        self.assertEqual(self.client.get("/hub/api/feedback/unread/").json(), {"unread_feedback_count": 0})
        self.client.force_login(User.objects.create_user("owner", password="pw", is_staff=True))
        self.assertEqual(self.client.get("/hub/api/feedback/unread/").json(), {"unread_feedback_count": 4})

    def test_bad_values_count_as_zero(self):
        self.client.force_login(User.objects.create_user("owner", password="pw", is_staff=True))
        for bad in ("4", -1, True, [4]):
            DatabaseStateBackend().set("feedback", "unread_count", bad)
            self.assertEqual(self.client.get("/hub/api/feedback/unread/").json()["unread_feedback_count"], 0)


class BuiltinRemovedTests(TransactionTestCase):
    """hub's 0001 migration clears out what the old built-in feedback app left behind."""

    migration = importlib.import_module("hub.migrations.0001_drop_builtin_feedback")

    def leftovers(self):
        ct = ContentType.objects.create(app_label="feedback", model="feedback")
        Permission.objects.create(content_type=ct, codename="view_feedback", name="Can view feedback")
        MigrationRecorder(connection).record_applied("feedback", "0004_feedback_stay")
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE feedback_feedback (id integer PRIMARY KEY, message text)")
            cursor.execute("INSERT INTO feedback_feedback (id, message) VALUES (1, 'old')")

    def run_migration(self):
        with connection.schema_editor() as editor:
            self.migration.drop_builtin_feedback(apps, editor)

    def test_drops_the_table_and_its_records(self):
        self.assertFalse(apps.is_installed("feedback"))
        self.leftovers()
        self.run_migration()
        self.run_migration()  # idempotent
        self.assertNotIn("feedback_feedback", connection.introspection.table_names())
        self.assertFalse(MigrationRecorder.Migration.objects.filter(app="feedback").exists())
        self.assertFalse(ContentType.objects.filter(app_label="feedback").exists())
        self.assertFalse(Permission.objects.filter(content_type__app_label="feedback").exists())

    def test_leaves_an_in_process_feedback_app_alone(self):
        self.leftovers()
        with mock.patch.object(self.migration.global_apps, "is_installed", return_value=True):
            self.run_migration()
        self.assertIn("feedback_feedback", connection.introspection.table_names())
        self.assertTrue(MigrationRecorder.Migration.objects.filter(app="feedback").exists())
        self.run_migration()