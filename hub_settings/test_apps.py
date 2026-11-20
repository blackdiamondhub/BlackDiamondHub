"""Settings -> Apps: the installed list, the store, installing and managing."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from broker import catalog, jobs, services
from broker.models import AppInstall
from broker.tests.base import reset_broker
from broker.tests.test_supervisor import SUPERVISOR, CatalogTestCase, SupervisorMixin, catalog_data, version

from .apps_views import describe


@override_settings(**SUPERVISOR)
class AppsPageTests(SupervisorMixin, CatalogTestCase, TestCase):
    def setUp(self):
        super().setUp()
        reset_broker()
        self.addCleanup(reset_broker)
        patcher = patch.object(services, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("broker.remote.fetch_manifest", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        jobs.inline = True
        self.addCleanup(setattr, jobs, "inline", False)
        jobs.clear()
        self.addCleanup(jobs.clear)
        User = get_user_model()
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.served = catalog_data(version(permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)

    def install(self, **kw):
        return self.client.post(reverse("hub_settings_store_app", args=["hello"]), kw)

    def test_staff_only(self):
        self.client.logout()
        for name, args in (("hub_settings_apps", []), ("hub_settings_store", []),
                           ("hub_settings_store_app", ["hello"]), ("hub_settings_app_manage", ["hello"]),
                           ("hub_settings_apps_live", [])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)
        self.assertEqual(self.client.post(reverse("hub_settings_app_uninstall", args=["hello"])).status_code, 302)

    def test_store_lists_catalog_apps_not_installed(self):
        page = self.client.get(reverse("hub_settings_store"))
        self.assertContains(page, "Hello")
        self.assertContains(page, "local network")
        sheet = self.client.get(reverse("hub_settings_store_app", args=["hello"]))
        self.assertContains(sheet, "Install Hello")
        self.assertContains(sheet, 'value="speakers" checked')
        self.assertContains(sheet, "trusted")
        self.install(perm=["alerts"])
        self.assertEqual(self.client.get(reverse("hub_settings_store")).context["apps"], [])

    def test_beta_badge_only_on_a_beta_hub(self):
        self.served = catalog_data(version("1.1.0b1", channel="beta"), version("1.0.0"))
        catalog.refresh(force=True)
        with self.settings(HUB_CHANNEL="stable"):
            self.assertNotContains(self.client.get(reverse("hub_settings_store")), "ap-pill beta")
            self.assertEqual(self.client.get(reverse("hub_settings_store_app", args=["hello"])).context["version"],
                             "1.0.0")
        with self.settings(HUB_CHANNEL="beta"):
            self.assertContains(self.client.get(reverse("hub_settings_store")), "v1.1.0b1<span class=\"ap-pill beta\">Beta</span>")
            sheet = self.client.get(reverse("hub_settings_store_app", args=["hello"]))
            self.assertContains(sheet, "v1.1.0b1<span class=\"ap-pill beta\">Beta</span>")
            self.install(perm=["alerts"])
            self.assertContains(self.client.get(reverse("hub_settings_apps")), "ap-pill beta")

    def test_install_grants_only_what_the_admin_chose(self):
        response = self.install(perm=["alerts", "not-asked"])
        self.assertRedirects(response, reverse("hub_settings_app_manage", args=["hello"]), fetch_redirect_response=False)
        record = AppInstall.objects.get()
        self.assertTrue(record.enabled)
        self.assertEqual((record.granted_permissions, record.approved_network), (["alerts"], "lan"))
        self.assertEqual(jobs.get("hello")["state"], jobs.DONE)

    def test_installed_and_manage_pages(self):
        self.install(perm=["alerts", "speakers"])
        page = self.client.get(reverse("hub_settings_apps"))
        self.assertContains(page, reverse("hub_settings_app_manage", args=["hello"]))
        manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertContains(manage, "line1")
        self.assertContains(manage, "Hold to uninstall")
        self.assertContains(manage, 'value="speakers" checked')

    def test_sharing_card_and_receives_line(self):
        from broker.models import ShareGrant

        self.install(perm=["alerts"])
        AppInstall.objects.filter(app_id="hello").update(manifest={
            "id": "hello", "name": "Hello", "shares": [{"name": "events", "type": "text/calendar", "title": "Lodge events"}]})
        AppInstall.objects.create(app_id="cal", runtime=AppInstall.CONTAINER, upstream="http://127.0.0.1:9202",
                                  manifest={"id": "cal", "name": "Calendar", "reads": ["text/calendar"]})
        AppInstall.objects.create(app_id="cams", runtime=AppInstall.CONTAINER, upstream="http://127.0.0.1:9203",
                                  manifest={"id": "cams", "name": "Cameras"})
        manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertContains(manage, 'id="sharing"')
        self.assertContains(manage, "Lodge events")
        self.assertContains(manage, "not published yet")
        self.assertContains(manage, "Show all apps")
        self.assertEqual([a["id"] for a in manage.context["sharing"][0]["readers"]], ["cal"])
        self.assertEqual([a["id"] for a in manage.context["sharing"][0]["others"]], ["cams"])
        ShareGrant.objects.create(consumer="cal", provider="hello", name="events")
        self.assertContains(self.client.get(reverse("hub_settings_app_manage", args=["cal"])),
                            "Lodge events (from Hello)")
    def test_installed_page_doesnt_wait_on_the_supervisor(self):
        self.install(perm=["alerts"])
        self.sup.requests.clear()
        page = self.client.get(reverse("hub_settings_apps"))
        self.assertEqual(self.sup.requests, [])
        self.assertContains(page, reverse("hub_settings_apps_live"))
        live = self.client.get(reverse("hub_settings_apps_live")).json()
        self.assertEqual((live["apps"]["hello"]["status"], live["apps"]["hello"]["dot"]), ("Sleeping", "y"))
        self.assertEqual((live["used_mb"], live["budget_mb"] > 0), (0, True))
        self.assertTrue(self.sup.requests)

    def test_permissions_auto_update_and_restart(self):
        self.install(perm=["alerts", "speakers"])
        self.client.post(reverse("hub_settings_app_permissions", args=["hello"]), {"perm": ["speakers"]})
        self.client.post(reverse("hub_settings_app_auto_update", args=["hello"]), {})
        record = AppInstall.objects.get()
        self.assertEqual((record.granted_permissions, record.auto_update), (["speakers"], False))
        self.client.post(reverse("hub_settings_app_restart", args=["hello"]))
        self.assertTrue(any(r.url.path == "/apps/hello/restart" for r in self.sup.requests))

    def widen(self, **kw):
        """Install 1.0.0, then serve a 2.0.0 that also asks for ``media``."""
        self.install(perm=["alerts", "speakers"])
        self.served = catalog_data(version("2.0.0", permissions=["alerts", "speakers", "media"], network="lan",
                                           changelog="Screen alerts", **kw),
                                   version(permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)

    def test_update_that_widens_is_reviewed_first(self):
        self.widen()
        page = self.client.get(reverse("hub_settings_apps"))
        self.assertContains(page, "Needs your approval")
        self.assertContains(page, "Asks for: Upload media")
        self.assertNotContains(page, "Update to v2.0.0")
        self.assertNotContains(page, "Update all")
        manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertContains(manage, 'id="review"')
        self.assertContains(manage, "Review update to v2.0.0")
        self.assertContains(manage, "Screen alerts")
        self.assertContains(manage, 'Upload media<span class="new">NEW</span>')
        self.assertContains(manage, "stays on v1.0.0")
        url = reverse("hub_settings_app_update", args=["hello"])
        response = self.client.post(url)
        self.assertRedirects(response, reverse("hub_settings_app_manage", args=["hello"]),
                             fetch_redirect_response=False)
        self.assertEqual(AppInstall.objects.get().version, "1.0.0")
        self.client.post(url, {"approve": "1"})
        record = AppInstall.objects.get()
        self.assertEqual(record.version, "2.0.0")
        self.assertIn("media", record.granted_permissions)
        manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertNotContains(manage, 'id="review"')
        self.assertContains(manage, "Updated to v2.0.0. Upload media is on.")

    def test_approved_update_nudges_to_settings(self):
        self.widen()
        spec = SimpleNamespace(id="hello", tiles=[], icon="", settings=[])
        with patch("hub_settings.apps_views._spec", return_value=spec), \
                patch("hub_settings.apps_views._label", return_value="Hello"), \
                patch("hub_settings.apps_views._icon", return_value=""), \
                patch("hub_settings.apps_views._settings_url", return_value="/settings/hello/"):
            self.client.post(reverse("hub_settings_app_update", args=["hello"]), {"approve": "1"})
            manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertContains(manage, "Check this app's settings")

    def test_review_shows_less_network_access(self):
        self.install(perm=["alerts", "speakers"])
        self.served = catalog_data(version("2.0.0", permissions=["alerts", "speakers", "media"], network="outbound"),
                                   version(permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)
        manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertContains(manage, 'Internet access only<span class="less">LESS</span>')
        self.assertContains(manage, "no longer needs local network")

    def test_update_from_the_list(self):
        self.install(perm=["alerts", "speakers"])
        self.served = catalog_data(version("1.1.0", permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)
        page = self.client.get(reverse("hub_settings_apps"))
        self.assertContains(page, "Update to v1.1.0")
        self.assertContains(page, "Update all (1)")
        self.assertNotContains(page, "Needs your approval")
        response = self.client.post(reverse("hub_settings_app_update", args=["hello"]), {"next": "apps"})
        self.assertRedirects(response, reverse("hub_settings_apps"), fetch_redirect_response=False)
        self.assertEqual(AppInstall.objects.get().version, "1.1.0")

    def test_update_all_names_apps_waiting_for_approval(self):
        self.widen()
        response = self.client.post(reverse("hub_settings_apps_update_all"), follow=True)
        self.assertContains(response, "Nothing to update without your approval. Hello needs your approval first.")
        self.assertEqual(AppInstall.objects.get().version, "1.0.0")

    def test_update_all(self):
        self.install(perm=["alerts", "speakers"])
        self.served = catalog_data(version("1.1.0", permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)
        self.client.post(reverse("hub_settings_apps_update_all"))
        self.assertEqual(AppInstall.objects.get().version, "1.1.0")

    def test_updated_badge_and_whats_new(self):
        self.install(perm=["alerts", "speakers"])
        self.assertIsNone(AppInstall.objects.get().version_changed)
        self.assertNotContains(self.client.get(reverse("hub_settings_apps")), ">Updated<")
        self.served = catalog_data(version("1.1.0", permissions=["alerts", "speakers"], network="lan",
                                           changelog="Faster tiles\n\n- fixed the popup"))
        catalog.refresh(force=True)
        self.client.post(reverse("hub_settings_app_update", args=["hello"]))
        page = self.client.get(reverse("hub_settings_apps"))
        self.assertContains(page, ">Updated<")
        self.assertContains(page, "#whats-new")
        manage = self.client.get(reverse("hub_settings_app_manage", args=["hello"]))
        self.assertContains(manage, "What's new in v1.1.0")
        self.assertContains(manage, "Faster tiles<br><br>- fixed the popup")
        AppInstall.objects.update(version_changed=timezone.now() - timedelta(days=8))
        self.assertNotContains(self.client.get(reverse("hub_settings_apps")), ">Updated<")

    def test_uninstall(self):
        self.install(perm=["alerts"])
        response = self.client.post(reverse("hub_settings_app_uninstall", args=["hello"]), {"purge": "on"})
        self.assertRedirects(response, reverse("hub_settings_apps"), fetch_redirect_response=False)
        self.assertFalse(AppInstall.objects.exists())
        delete = next(r for r in self.sup.requests if r.method == "DELETE")
        self.assertEqual(delete.url.params["purge_data"], "true")

    def test_install_blocked_when_memory_budget_is_full(self):
        sheet = self.client.get(reverse("hub_settings_store_app", args=["hello"]))
        self.assertNotContains(sheet, "budget")
        self.assertNotContains(sheet, "Not enough memory")
        with override_settings(BDH_APP_MEMORY_BUDGET_MB=50):
            sheet = self.client.get(reverse("hub_settings_store_app", args=["hello"]))
            self.assertContains(sheet, "Not enough memory")
            self.assertContains(sheet, 'type="submit" disabled')
            self.install(perm=["alerts"])
        self.assertFalse(AppInstall.objects.exists())

    def test_unknown_app_is_404(self):
        self.assertEqual(self.client.get(reverse("hub_settings_store_app", args=["nope"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("hub_settings_app_manage", args=["nope"])).status_code, 404)

    def test_describe(self):
        self.assertEqual(describe("ha.read:sensor.*")["detail"], "sensor.*")
        self.assertEqual(describe("events.subscribe:#")["detail"], "everything")
        self.assertEqual(describe("weird")["title"], "weird")
