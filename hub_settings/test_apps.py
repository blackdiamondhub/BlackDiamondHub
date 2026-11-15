"""Settings -> Apps: the installed list, the store, installing and managing."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

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
                           ("hub_settings_store_app", ["hello"]), ("hub_settings_app_manage", ["hello"])):
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

    def test_permissions_auto_update_and_restart(self):
        self.install(perm=["alerts", "speakers"])
        self.client.post(reverse("hub_settings_app_permissions", args=["hello"]), {"perm": ["speakers"]})
        self.client.post(reverse("hub_settings_app_auto_update", args=["hello"]), {})
        record = AppInstall.objects.get()
        self.assertEqual((record.granted_permissions, record.auto_update), (["speakers"], False))
        self.client.post(reverse("hub_settings_app_restart", args=["hello"]))
        self.assertTrue(any(r.url.path == "/apps/hello/restart" for r in self.sup.requests))

    def test_update_that_widens_is_reviewed_first(self):
        self.install(perm=["alerts", "speakers"])
        self.served = catalog_data(version("2.0.0", permissions=["alerts", "speakers", "media"], network="lan"),
                                   version(permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)
        self.assertContains(self.client.get(reverse("hub_settings_apps")), "Update v2.0.0")
        url = reverse("hub_settings_app_update", args=["hello"])
        response = self.client.post(url)
        review = reverse("hub_settings_app_manage", args=["hello"]) + "?review=2.0.0"
        self.assertRedirects(response, review, fetch_redirect_response=False)
        self.assertEqual(AppInstall.objects.get().version, "1.0.0")
        self.assertContains(self.client.get(review), "asks for more access")
        self.client.post(url, {"approve": "1"})
        record = AppInstall.objects.get()
        self.assertEqual(record.version, "2.0.0")
        self.assertIn("media", record.granted_permissions)

    def test_update_all(self):
        self.install(perm=["alerts", "speakers"])
        self.served = catalog_data(version("1.1.0", permissions=["alerts", "speakers"], network="lan"))
        catalog.refresh(force=True)
        self.client.post(reverse("hub_settings_apps_update_all"))
        self.assertEqual(AppInstall.objects.get().version, "1.1.0")

    def test_uninstall(self):
        self.install(perm=["alerts"])
        response = self.client.post(reverse("hub_settings_app_uninstall", args=["hello"]), {"purge": "on"})
        self.assertRedirects(response, reverse("hub_settings_apps"), fetch_redirect_response=False)
        self.assertFalse(AppInstall.objects.exists())
        delete = next(r for r in self.sup.requests if r.method == "DELETE")
        self.assertEqual(delete.url.params["purge_data"], "true")

    def test_unknown_app_is_404(self):
        self.assertEqual(self.client.get(reverse("hub_settings_store_app", args=["nope"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("hub_settings_app_manage", args=["nope"])).status_code, 404)

    def test_describe(self):
        self.assertEqual(describe("ha.read:sensor.*")["detail"], "sensor.*")
        self.assertEqual(describe("events.subscribe:#")["detail"], "everything")
        self.assertEqual(describe("weird")["title"], "weird")
