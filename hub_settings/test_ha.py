from unittest import mock

import requests
from bdh_sdk import ha as sdk_ha
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import ha, vault
from .models import HAInstance

KEY = b"\x07" * 32
TOKEN = "a-long-lived-token"


class HACase(TestCase):
    def setUp(self):
        vault.set_provider(vault.StaticKeyProvider(("k1", KEY)))
        self.addCleanup(vault.set_provider, None)
        ha.clear()
        self.addCleanup(ha.clear)

    def add(self, id, url="http://a:8123", default=False, order=0, token=TOKEN, **kw):
        row = HAInstance.objects.create(id=id, name=kw.pop("name", id.title()), url=url,
                                        is_default=default, order=order, **kw)
        if token:
            vault.set_secret(ha.VAULT_OWNER, ha.token_name(id), token)
        return row


class ProviderTests(HACase):
    def test_nothing_saved_means_no_instance(self):
        with mock.patch.dict("os.environ", {"HOMEASSISTANT_URL": "http://env:8123",
                                            "HOMEASSISTANT_ACCESS_TOKEN": "env-tok"}):
            self.assertEqual(ha.instances(), [])
            self.assertIsNone(ha.default_id())
            for instance in (None, "ha"):
                with self.subTest(instance=instance), self.assertRaises(sdk_ha.HAError):
                    ha.connection(instance)

    def test_saved_rows(self):
        self.add("home", url="http://home:8123")
        self.add("cabin", url="http://cabin:8123", default=True, order=1, site="Sun Peaks", token="cabin-tok")
        self.assertEqual([i["id"] for i in ha.instances() if i["default"]], ["cabin"])
        self.assertEqual(ha.connection(), ("http://cabin:8123", "cabin-tok"))
        self.assertEqual(ha.connection("home"), ("http://home:8123", TOKEN))
        with self.assertRaises(sdk_ha.HAError):
            ha.connection("ha")

    def test_first_row_is_default_when_none_marked(self):
        self.add("home")
        self.assertTrue(ha.instances()[0]["default"])

    def test_missing_token_is_blank(self):
        self.add("home", default=True, token=None)
        self.assertEqual(ha.connection(), ("http://a:8123", ""))

    def test_token_change_clears_cache(self):
        self.add("home", default=True)
        self.assertEqual(ha.connection()[1], TOKEN)
        vault.set_secret(ha.VAULT_OWNER, ha.token_name("home"), "new-token")
        self.assertEqual(ha.connection()[1], "new-token")

    def test_sdk_uses_hub_provider(self):
        self.add("home", default=True)
        self.assertEqual(sdk_ha.instances()[0]["id"], "home")


class SettingsPageTests(HACase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.guest = User.objects.create_user("guest", password="pw")
        self.url = reverse("hub_settings_ha")
        self.client.force_login(self.staff)

    def post(self, data):
        return self.client.post(self.url, data)

    def fields(self, id, name, url, site="", token=""):
        return {f"{id}__name": name, f"{id}__site": site, f"{id}__url": url, f"{id}__token": token}

    def test_staff_only(self):
        self.client.force_login(self.guest)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
        self.assertNotEqual(self.client.post(reverse("hub_settings_ha_test"), {}).status_code, 200)

    def test_get_offers_first_instance(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.context["new"]["id"], resp.context["new"]["url"]), ("ha", ""))
        self.assertContains(resp, "isn't set up yet")

    def test_first_save(self):
        resp = self.post({"new__id": "ha", **self.fields("new", "Home", "http://h:8123", token="t-1")})
        self.assertEqual(resp.status_code, 302)
        row = HAInstance.objects.get()
        self.assertEqual((row.id, row.url, row.is_default, row.updated_by), ("ha", "http://h:8123", True, self.staff))
        self.assertEqual(ha.connection(), ("http://h:8123", "t-1"))

    def test_first_save_needs_token(self):
        resp = self.post({"new__id": "ha", **self.fields("new", "Home", "http://other:8123")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("token", resp.context["new"]["error"])
        self.assertFalse(HAInstance.objects.exists())

    def test_tokens_are_never_shown(self):
        self.add("home", default=True)
        resp = self.client.get(self.url)
        self.assertNotContains(resp, TOKEN)
        self.assertTrue(resp.context["cards"][0]["token_set"])

    def test_edit_and_url_change_needs_token(self):
        self.add("home", default=True)
        resp = self.post(self.fields("home", "Home", "http://moved:8123"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("token", resp.context["cards"][0]["error"])
        self.assertEqual(resp.context["cards"][0]["url"], "http://moved:8123")
        resp = self.post(self.fields("home", "Lodge", "http://moved:8123/", site="MI", token="moved-tok"))
        self.assertEqual(resp.status_code, 302)
        row = HAInstance.objects.get()
        self.assertEqual((row.name, row.url, row.site), ("Lodge", "http://moved:8123", "MI"))
        self.assertEqual(ha.connection(), ("http://moved:8123", "moved-tok"))

    def test_add_instance_and_switch_default(self):
        self.add("home", default=True)
        resp = self.post({**self.fields("home", "Home", "http://a:8123"),
                          "new__id": "cabin", **self.fields("new", "Cabin", "https://cabin:8123", token="c-tok"),
                          "default": "new"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual([i["id"] for i in ha.instances() if i["default"]], ["cabin"])
        self.assertEqual(ha.connection("cabin"), ("https://cabin:8123", "c-tok"))
        self.assertEqual(ha.connection("home")[1], TOKEN)
        self.post({**self.fields("home", "Home", "http://a:8123"), **self.fields("cabin", "Cabin", "https://cabin:8123"),
                   "default": "home"})
        self.assertEqual(ha.default_id(), "home")

    def test_names_must_differ(self):
        # Apps label each instance by name; two "Home Assistant"s looked the same.
        self.add("home", default=True)
        self.add("cabin", order=1)
        resp = self.post({**self.fields("home", "Home Assistant", "http://a:8123"),
                          **self.fields("cabin", "home assistant ", "http://a:8123")})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("name", resp.context["cards"][0]["error"])
        self.assertIn("name", resp.context["cards"][1]["error"])
        resp = self.post({**self.fields("home", "Home", "http://a:8123"), **self.fields("cabin", "Cabin", "http://a:8123"),
                          "new__id": "barn", **self.fields("new", "CABIN", "http://b:1", token="t")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.context["new"]["error"])
        self.assertEqual(HAInstance.objects.count(), 2)

    def test_new_id_rules(self):
        self.add("home", default=True)
        base = {**self.fields("home", "Home", "http://a:8123")}
        for bad in ("Home", "1st", "home", "hub", "a-b", "x" * 41):
            with self.subTest(id=bad):
                resp = self.post({**base, "new__id": bad, **self.fields("new", "X", "http://x:1", token="t")})
                self.assertEqual(resp.status_code, 400)
                self.assertIn("id", resp.context["new"]["error"])
        self.assertEqual(HAInstance.objects.count(), 1)

    def test_new_id_cant_be_a_reserved_source(self):
        self.add("home", default=True)
        with mock.patch("hub.events.reserved_names", return_value={"hook"}):
            resp = self.post({**self.fields("home", "Home", "http://a:8123"),
                              "new__id": "hook", **self.fields("new", "X", "http://x:1", token="t")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("id", resp.context["new"]["error"])

    def test_bad_url(self):
        self.add("home", default=True)
        for bad in ("ftp://a", "homeassistant", "http://a:8123/?x=1", ""):
            with self.subTest(url=bad):
                resp = self.post({**self.fields("home", "Home", bad, token="t")})
                self.assertEqual(resp.status_code, 400)
                self.assertIn("url", resp.context["cards"][0]["error"])

    def test_remove_repicks_default(self):
        self.add("home", default=True)
        self.add("cabin", order=1)
        resp = self.post({"remove": "home"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(list(HAInstance.objects.values_list("id", "is_default")), [("cabin", True)])
        self.assertNotIn(ha.token_name("home"), vault.names(ha.VAULT_OWNER))
        self.assertEqual(ha.default_id(), "cabin")

    def test_remove_last_leaves_none(self):
        self.add("home", default=True)
        self.post({"remove": "home"})
        with self.assertRaises(sdk_ha.HAError):
            ha.connection()

    def test_clear_token(self):
        self.add("home", default=True)
        self.assertEqual(self.post({"clear_token": "home"}).status_code, 302)
        self.assertEqual(ha.connection(), ("http://a:8123", ""))
        self.assertTrue(HAInstance.objects.filter(pk="home").exists())

    def test_no_vault_key(self):
        vault.set_provider(vault.StaticKeyProvider())
        resp = self.post({"new__id": "ha", **self.fields("new", "Home", "http://x:8123", token="t")})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(HAInstance.objects.exists())

    def test_save_resyncs_event_sources(self):
        with mock.patch("hub.events.sync_instances") as sync:
            self.post({"new__id": "ha", **self.fields("new", "Home", "http://h:8123", token="t")})
        sync.assert_called_once_with()


class ConnectionTestTests(HACase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.url = reverse("hub_settings_ha_test")

    def check(self, data, config=None, status=200, states=None):
        conf = mock.Mock(status_code=status, json=mock.Mock(return_value=config or {"location_name": "Lodge",
                                                                                    "version": "2026.11"}))
        st = mock.Mock(status_code=200, json=mock.Mock(return_value=states or [{}, {}]))
        with mock.patch("requests.get", side_effect=[conf, st]) as get:
            body = self.client.post(self.url, data).json()
        return body, get

    def test_ok_with_entered_token(self):
        body, get = self.check({"id": "", "url": "http://x:8123/", "token": "typed"})
        self.assertEqual(body, {"ok": True, "message": "Connected to Lodge (2026.11), 2 entities."})
        self.assertEqual(get.call_args_list[0].args[0], "http://x:8123/api/config")
        self.assertEqual(get.call_args_list[0].kwargs["headers"], {"Authorization": "Bearer typed"})

    def test_saved_token_only_for_saved_url(self):
        self.add("home", url="http://a:8123", default=True)
        body, get = self.check({"id": "home", "url": "http://a:8123"})
        self.assertTrue(body["ok"])
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})
        with mock.patch("requests.get") as get:
            body = self.client.post(self.url, {"id": "home", "url": "http://evil:8123"}).json()
        self.assertFalse(body["ok"])
        get.assert_not_called()

    def test_unsaved_instance_needs_token(self):
        with mock.patch("requests.get") as get:
            body = self.client.post(self.url, {"id": "ha", "url": "http://h:8123"}).json()
        self.assertFalse(body["ok"])
        get.assert_not_called()

    def test_failures(self):
        body, _ = self.check({"url": "http://x:1", "token": "t"}, status=401)
        self.assertEqual(body["message"], "Home Assistant refused the token.")
        with mock.patch("requests.get", side_effect=requests.ConnectionError()):
            body = self.client.post(self.url, {"url": "http://x:1", "token": "t"}).json()
        self.assertEqual(body, {"ok": False, "message": "Can't reach it (ConnectionError)."})
        body = self.client.post(self.url, {"url": "file:///etc", "token": "t"}).json()
        self.assertFalse(body["ok"])

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)


class StatusTests(HACase):
    def setUp(self):
        super().setUp()
        from . import ha_views

        ha_views._probes.clear()
        self.addCleanup(ha_views._probes.clear)
        self.staff = get_user_model().objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.url = reverse("hub_settings_ha_status")

    def status(self, live=None, **get):
        with mock.patch("hub.events.instance_status", side_effect=lambda i: (live or {}).get(i)), \
                mock.patch("requests.get", **get) as req:
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["instances"], req

    def test_live_connection_wins(self):
        self.add("home", default=True)
        got, req = self.status({"home": "reconnecting"})
        self.assertEqual(got, {"home": "reconnecting"})
        req.assert_not_called()

    def test_rest_probe_when_not_live(self):
        self.add("home", url="http://home:8123", default=True)
        self.add("cabin", url="http://cabin:8123", order=1)
        self.add("bare", order=2, token=None)
        replies = {"http://home:8123/api/config": 200, "http://cabin:8123/api/config": 401}
        got, req = self.status({"home": "idle"},
                               side_effect=lambda u, **kw: mock.Mock(status_code=replies[u]))
        self.assertEqual(got, {"home": "connected", "cabin": "bad_token", "bare": "no_token"})
        self.assertEqual(req.call_args.kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})
        got, req = self.status(side_effect=AssertionError("cached"))
        self.assertEqual(got["cabin"], "bad_token")

    def test_unreachable(self):
        self.add("home", default=True)
        got, _ = self.status(side_effect=requests.ConnectionError())
        self.assertEqual(got, {"home": "unreachable"})

    def test_staff_get_only(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.client.force_login(get_user_model().objects.create_user("guest", password="pw"))
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_page_has_status_pill(self):
        self.add("home", default=True)
        self.assertContains(self.client.get(reverse("hub_settings_ha")), "data-status")