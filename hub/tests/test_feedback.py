from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from broker import remote
from feedback.models import Feedback
from hub import feedback
from hub_settings.state import DatabaseStateBackend


def features(**flags):
    from django.conf import settings

    return override_settings(HUB_FEATURES={**settings.HUB_FEATURES, **flags})


class TopBarTests(TestCase):
    """The pill, menu items and modal show only when feedback has somewhere to go."""

    def setUp(self):
        self.staff = User.objects.create_user("owner", password="pw", is_staff=True)

    def test_builtin_app_drives_the_top_bar(self):
        Feedback.objects.create(message="hi")
        self.client.force_login(self.staff)
        page = self.client.get("/").content.decode()
        self.assertIn('id="feedback-button"', page)
        self.assertIn(f'action="{reverse("submit_feedback")}"', page)
        self.assertIn(f'href="{reverse("view_feedback")}" role="menuitem" id="inbox-link"', page)
        self.assertIn('title="Unread feedback">1<', page)

    @features(feedback=False)
    def test_site_can_turn_it_off(self):
        self.client.force_login(self.staff)
        page = self.client.get("/").content.decode()
        for marker in ('id="feedback-button"', 'id="feedback-modal"', 'class="hub-menu-fb"', 'id="inbox-link"'):
            self.assertNotIn(marker, page)
        self.assertIn('id="user-menu"', page)

    @features(feedback=False)
    def test_container_app_takes_over(self):
        DatabaseStateBackend().set("feedback", "unread_count", 3)
        self.client.force_login(self.staff)
        with mock.patch.object(feedback, "container_enabled", return_value=True):
            page = self.client.get("/").content.decode()
        self.assertIn('action="/hub/api/feedback/submit/"', page)
        self.assertIn(f'href="{remote.page_url("feedback", "/feedback/")}" role="menuitem"', page)
        self.assertIn('title="Unread feedback">3<', page)

    def test_guests_see_the_pill_but_no_count(self):
        Feedback.objects.create(message="hi")
        page = self.client.get("/").content.decode()
        self.assertIn('id="feedback-button"', page)
        self.assertNotIn("Unread feedback", page)


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


class SiteConfigTests(TestCase):
    def test_only_mercer_island_turns_the_builtin_off(self):
        import tomllib
        from pathlib import Path

        from django.conf import settings

        root = Path(settings.BASE_DIR) / "sites"
        mi = tomllib.loads((root / "mercerisland" / "site.toml").read_text(encoding="utf-8"))
        sp = tomllib.loads((root / "sunpeaks" / "site.toml").read_text(encoding="utf-8"))
        self.assertIs(mi["features"]["feedback"], False)
        self.assertNotIn("feedback", sp.get("features", {}))