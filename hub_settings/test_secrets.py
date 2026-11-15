import os
import stat
import sys
import tempfile
from io import StringIO

import bdh_sdk.secrets as sdk_secrets
from bdh_sdk import appdata
from bdh_sdk.plugins import SECRET
from bdh_sdk.settings import app_setting
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from hub.plugins import PluginSpec, Setting

from . import store, vault
from .models import AppSecret, AppSetting

KEY1, KEY2 = b"\x01" * 32, b"\x02" * 32
VAULTED = PluginSpec(
    id="vaulted",
    app="vaulted",
    settings=(
        Setting("title", "Title", default="Hello"),
        Setting("api_key", "API key", type=SECRET),
    ),
)
PLUGINS = tuple(settings.HUB_PLUGINS) + (VAULTED,)
VALUE = "hunter2-sekrit-value"


class VaultCase(TestCase):
    def setUp(self):
        self.keys = vault.StaticKeyProvider(("k1", KEY1))
        vault.set_provider(self.keys)
        self.addCleanup(vault.set_provider, None)
        store.clear()
        self.addCleanup(store.clear)


class CryptoTests(VaultCase):
    def test_round_trip_and_fresh_nonce(self):
        a = vault.encrypt("cams", "pw", VALUE)
        b = vault.encrypt("cams", "pw", VALUE)
        self.assertEqual(a[0], "k1")
        self.assertNotEqual(a[1], b[1])
        self.assertNotIn(VALUE.encode(), a[2])
        self.assertEqual(vault.decrypt("cams", "pw", *a), VALUE)

    def test_wrong_associated_data_fails(self):
        kid, nonce, ct = vault.encrypt("cams", "pw", VALUE)
        for app, name in (("other", "pw"), ("cams", "pw2")):
            with self.assertRaises(vault.SecretStoreError) as err:
                vault.decrypt(app, name, kid, nonce, ct)
            self.assertNotIn(VALUE, str(err.exception))

    def test_row_copied_to_another_app_fails(self):
        vault.set_secret("cams", "pw", VALUE)
        row = AppSecret.objects.get(app_id="cams", name="pw")
        AppSecret.objects.create(app_id="thief", name="pw", key_id=row.key_id, nonce=row.nonce,
                                 ciphertext=row.ciphertext)
        with self.assertRaises(vault.SecretStoreError):
            vault.get_secret("thief", "pw")
        self.assertEqual(vault.get_secret("cams", "pw"), VALUE)

    def test_store_api(self):
        with self.assertRaises(KeyError):
            vault.get_secret("cams", "pw")
        vault.set_secret("cams", "pw", VALUE)
        vault.set_secret("cams", "pw", "new")
        self.assertEqual(vault.get_secret("cams", "pw"), "new")
        self.assertEqual(vault.names("cams"), {"pw"})
        self.assertEqual(vault.names("other"), set())
        self.assertTrue(vault.delete_secret("cams", "pw"))
        self.assertFalse(vault.delete_secret("cams", "pw"))
        with self.assertRaises(ValueError):
            vault.set_secret("cams", "Bad Name", VALUE)
        with self.assertRaises(ValueError):
            vault.set_secret("cams", "pw", "")

    def test_rotation(self):
        vault.set_secret("cams", "pw", VALUE)
        vault.set_secret("wifi", "token", "t0k")
        self.keys = vault.StaticKeyProvider(("k1", KEY1), ("k2", KEY2))
        vault.set_provider(self.keys)
        vault.set_secret("wifi", "new", "n")  # already on k2
        out = StringIO()
        call_command("rotate_secrets_key", stdout=out)
        self.assertIn("Re-encrypted 2", out.getvalue())
        self.assertEqual(set(AppSecret.objects.values_list("key_id", flat=True)), {"k2"})
        vault.set_provider(vault.StaticKeyProvider(("k2", KEY2)))  # old key removed
        self.assertEqual(vault.get_secret("cams", "pw"), VALUE)
        self.assertEqual(vault.get_secret("wifi", "token"), "t0k")
        self.assertEqual(vault.rotate(), (0, 3))

    def test_missing_key_is_a_clean_error(self):
        vault.set_secret("cams", "pw", VALUE)
        vault.set_provider(vault.StaticKeyProvider(("k9", KEY2)))
        with self.assertRaises(vault.SecretStoreError):
            vault.get_secret("cams", "pw")
        with tempfile.TemporaryDirectory() as tmp:
            vault.set_provider(vault.FileKeyProvider(os.path.join(tmp, "nope.key")))
            self.assertFalse(vault.available())
            with self.assertRaises(vault.SecretStoreError):
                vault.set_secret("cams", "pw", VALUE)

    def test_key_file_format(self):
        text = "# comment\n\n" + vault.key_line("a", KEY1) + vault.key_line("b", KEY2)
        self.assertEqual(vault.parse_keys(text), [("a", KEY1), ("b", KEY2)])
        for bad in ("a:short\n", "bad id:" + vault.key_line("x", KEY1).split(":")[1],
                    vault.key_line("a", KEY1) * 2):
            with self.assertRaises(vault.SecretStoreError):
                vault.parse_keys(bad)

    def test_sdk_in_process_backend(self):
        sdk_secrets.set("pw", VALUE, app="cams")
        self.assertEqual(sdk_secrets.get("pw", app="cams"), VALUE)
        self.assertIsNone(sdk_secrets.get("pw", app="other"))
        self.assertTrue(AppSecret.objects.filter(app_id="cams", name="pw").exists())
        sdk_secrets.delete("pw", app="cams")
        self.assertIsNone(sdk_secrets.get("pw", app="cams"))

    def test_not_in_repr_or_appdata_export(self):
        vault.set_secret("feedback", "pw", VALUE)
        row = AppSecret.objects.get()
        self.assertNotIn(VALUE, repr(row) + str(row))
        out = StringIO()
        appdata.export_rows("feedback", out)
        self.assertNotIn("appsecret", out.getvalue().lower())
        self.assertNotIn(VALUE, out.getvalue())
        out = StringIO()
        appdata.export_rows("hub_settings", out)  # the hub's own dump: ciphertext only
        self.assertIn("hub_settings.appsecret", out.getvalue())
        self.assertNotIn(VALUE, out.getvalue())


class KeyFileCommandTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "secrets.key")

    def test_generate_refuses_to_overwrite_and_appends(self):
        out = StringIO()
        call_command("generate_secrets_key", path=self.path, key_id="k1", stdout=out)
        with open(self.path) as fh:
            first = fh.read()
        self.assertNotIn(first.strip().splitlines()[-1].split(":")[1], out.getvalue())
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with self.assertRaises(CommandError):
            call_command("generate_secrets_key", path=self.path, key_id="k1", stdout=StringIO())
        with open(self.path) as fh:
            self.assertEqual(fh.read(), first)
        with self.assertRaises(CommandError):
            call_command("generate_secrets_key", path=self.path, key_id="k1", append=True, stdout=StringIO())
        call_command("generate_secrets_key", path=self.path, key_id="k2", append=True, stdout=StringIO())
        provider = vault.FileKeyProvider(self.path)
        self.assertEqual(provider.current()[0], "k2")
        self.assertEqual(set(provider.keys()), {"k1", "k2"})

    def test_append_needs_a_file(self):
        with self.assertRaises(CommandError):
            call_command("generate_secrets_key", path=self.path, append=True, stdout=StringIO())


class MigratePlainSecretsTests(VaultCase):
    def setUp(self):
        super().setUp()
        AppSetting.objects.create(app_id="cameras", key="protect_password_1", value=VALUE)
        AppSetting.objects.create(app_id="cameras", key="protect_api_key_2", value="api")
        AppSetting.objects.create(app_id="cameras", key="protect_username_1", value="viewer")
        AppSetting.objects.create(app_id="restaurants", key="google_places_api_key", value="")
        AppSetting.objects.create(app_id="wifi", key="password_1", value="guestpass")

    def run_cmd(self, **kw):
        out = StringIO()
        call_command("migrate_plain_secrets", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self.run_cmd(dry_run=True)
        self.assertIn("would move to secret store: cameras.protect_password_1", out)
        self.assertNotIn(VALUE, out)
        self.assertEqual(AppSetting.objects.count(), 5)
        self.assertFalse(AppSecret.objects.exists())

    def test_moves_once_and_is_idempotent(self):
        out = self.run_cmd()
        self.assertNotIn(VALUE, out)
        self.assertIn("Moved 2", out)
        self.assertEqual(vault.get_secret("cameras", "protect_password_1"), VALUE)
        self.assertEqual(vault.get_secret("cameras", "protect_api_key_2"), "api")
        self.assertEqual(set(AppSetting.objects.values_list("app_id", "key")),
                         {("cameras", "protect_username_1"), ("wifi", "password_1")})
        out = self.run_cmd()
        self.assertIn("Moved 0", out)
        self.assertEqual(AppSecret.objects.count(), 2)
        self.assertEqual(vault.get_secret("cameras", "protect_password_1"), VALUE)

    def test_existing_secret_wins(self):
        vault.set_secret("cameras", "protect_password_1", "newer")
        self.run_cmd(app=["cameras"])
        self.assertEqual(vault.get_secret("cameras", "protect_password_1"), "newer")
        self.assertFalse(AppSetting.objects.filter(key="protect_password_1").exists())
        self.assertTrue(AppSetting.objects.filter(app_id="restaurants").exists())

    def test_app_setting_still_finds_moved_values(self):
        self.run_cmd()
        store.clear()
        self.assertEqual(app_setting("cameras", "protect_password_1"), VALUE)
        self.assertEqual(app_setting("cameras", "protect_username_1"), "viewer")

    def test_needs_a_key_and_a_known_app(self):
        vault.set_provider(vault.StaticKeyProvider())
        with self.assertRaises(CommandError):
            self.run_cmd()
        self.assertEqual(AppSetting.objects.count(), 5)
        with self.assertRaises(CommandError):
            self.run_cmd(app=["nope"], dry_run=True)


@override_settings(HUB_PLUGINS=PLUGINS)
class SecretSettingsPageTests(VaultCase):
    url = "/settings/app/vaulted/"

    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_write_only(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn("not set", page)
        self.assertIn('type="password"', page)
        resp = self.client.post(self.url, {"title": "Hello", "api_key": VALUE})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(vault.get_secret("vaulted", "api_key"), VALUE)
        self.assertFalse(AppSetting.objects.filter(app_id="vaulted", key="api_key").exists())
        page = self.client.get(self.url).content.decode()
        self.assertIn("&bull;&bull;&bull;&bull; set", page)
        self.assertIn('name="clear_secret" value="api_key"', page)
        self.assertNotIn(VALUE, page)

    def test_blank_keeps_and_clear_deletes(self):
        vault.set_secret("vaulted", "api_key", VALUE)
        self.client.post(self.url, {"title": "Changed", "api_key": ""})
        self.assertEqual(vault.get_secret("vaulted", "api_key"), VALUE)
        self.client.post(self.url, {"clear_secret": "api_key"})
        self.assertEqual(vault.names("vaulted"), set())
        self.client.post(self.url, {"clear_secret": "title"})  # not a secret: ignored
        self.assertEqual(app_setting("vaulted", "title", "Hello"), "Changed")

    def test_error_page_does_not_echo_it(self):
        resp = self.client.post(self.url, {"title": "Hello", "api_key": "x" * 9000})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("x" * 100, resp.content.decode())
        self.assertFalse(AppSecret.objects.exists())

    def test_no_key_shows_error(self):
        vault.set_provider(vault.StaticKeyProvider())
        resp = self.client.post(self.url, {"title": "Hello", "api_key": VALUE})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no key", resp.content.decode())
        self.assertNotIn(VALUE, resp.content.decode())

    def test_legacy_plain_setting_is_write_only(self):
        spec = PluginSpec(id="cameras", app="cameras",
                          settings=(Setting("protect_password_1", "Password"),))
        AppSetting.objects.create(app_id="cameras", key="protect_password_1", value=VALUE)
        with self.settings(HUB_PLUGINS=tuple(p for p in PLUGINS if p.id != "cameras") + (spec,)):
            page = self.client.get("/settings/app/cameras/").content.decode()
            self.assertNotIn(VALUE, page)
            self.assertIn("&bull;&bull;&bull;&bull; set", page)
            self.client.post("/settings/app/cameras/", {"protect_password_1": "rotated"})
        self.assertEqual(vault.get_secret("cameras", "protect_password_1"), "rotated")
        self.assertFalse(AppSetting.objects.filter(app_id="cameras").exists())
