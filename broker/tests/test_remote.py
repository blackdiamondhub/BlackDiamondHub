import copy
import json
from unittest import mock

from bdh_sdk.identity import verify
from django.core.cache import cache
from django.test import TestCase, override_settings

from broker import remote
from broker.tokens import token_for
from hub import modes, status

from .base import make_install, manifest, reset_broker

STATUS = {"text": "2 cameras", "level": "info",
          "rows": [{"id": "front", "title": "Front", "detail": "", "image": "/cameras/snap/front.jpg",
                    "dimmed": False, "actions": [{"id": "snap", "icon": "fas fa-camera", "label": "Snap",
                                                   "primary": True}]}]}
SHORTCUTS = [{"id": "lights", "title": "Lights", "icon": "fas fa-lightbulb", "order": 1,
              "modes": ["dashboard", "guest", "owner"]}]


class FakeApp:
    """Stands in for the app's runtime behind ``requests.request``."""

    def __init__(self, install, replies=None):
        self.token = token_for(install)
        self.replies = replies or {}
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        path = url.split("9201", 1)[1]
        headers = dict(headers or {})
        assert verify(self.token, method, path, headers, body=data or b"") is not None, "bad signature"
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        resp = mock.Mock(status_code=200)
        if path == "/_bdh/manifest":
            resp.json.return_value = copy.deepcopy(self.replies.get("manifest"))
        else:
            resp.json.return_value = {"result": copy.deepcopy(self.replies.get(body["hook"]))}
        return resp


@override_settings(HUB_PLUGINS=())
class RemoteSpecTests(TestCase):
    def setUp(self):
        reset_broker()
        cache.clear()
        self.addCleanup(reset_broker)
        self.install = make_install()

    def spec(self):
        return remote.spec_for("cams")

    def test_spec_from_manifest(self):
        spec = self.spec()
        self.assertTrue(spec.remote)
        self.assertEqual(spec.prefix, "apps/cams/")
        self.assertEqual([t.url for t in spec.tiles], ["/apps/cams/cameras/"])
        self.assertEqual(spec.tiles[0].icon, "/apps/cams/_raw/static/cams/icon.png")
        self.assertEqual(spec.status, "remote:cams:status")
        self.assertIsNone(spec.stay)
        self.assertEqual([p.url for p in spec.settings_pages], ["/apps/cams/cameras/setup/"])
        self.assertEqual([s.key for s in spec.settings], ["grid", "speaker"])

    def test_catalog_has_remote_tile(self):
        lodge = modes._catalog("guest")["lodge"]
        tile = next(t for t in lodge if t["plugin"] == "cams")
        self.assertEqual(tile["id"], "cams:cameras:index")
        self.assertEqual(tile["url"], "/apps/cams/cameras/")
        self.assertFalse(any("evil" in t["url"] for t in lodge))

    def test_disabled_app_disappears(self):
        self.assertIsNotNone(self.spec())
        self.install.enabled = False
        self.install.save()
        self.assertIsNone(self.spec())

    def test_clash_with_in_process_app_is_ignored(self):
        local = mock.Mock(id="cams", tiles=())
        with override_settings(HUB_PLUGINS=(local,)):
            remote.installs_changed()
            self.assertEqual(modes.plugins(), (local,))

    def test_status_and_shortcuts_pass_through_converters(self):
        app = FakeApp(self.install, {"status": STATUS, "shortcuts": SHORTCUTS})
        with mock.patch("broker.remote.requests.request", side_effect=app):
            data = status.home_data("guest")
        got = data["status"]["cams"]
        self.assertEqual(got["text"], "2 cameras")
        self.assertEqual(got["rows"][0]["image"], "/apps/cams/_raw/cameras/snap/front.jpg")
        self.assertEqual(got["rows"][0]["actions"], STATUS["rows"][0]["actions"])
        self.assertEqual([sc["id"] for sc in data["shortcuts"]], ["lights"])
        hooks = {body["hook"]: body for _m, _p, body in app.calls}
        self.assertEqual(hooks["status"]["mode"], "guest")

    def test_run_shortcut_and_action(self):
        app = FakeApp(self.install, {"status": STATUS, "shortcuts": SHORTCUTS,
                                     "run_shortcut": "Done", "run_action": "Snapped"})
        with mock.patch("broker.remote.requests.request", side_effect=app):
            self.assertEqual(status.run_shortcut("cams", "lights", "guest"), "Done")
            self.assertEqual(status.run_action("cams", "front", "snap", "guest"), "Snapped")
            with self.assertRaises(status.ActionNotFound):
                status.run_action("cams", "front", "nope", "guest")
        bodies = [b for _m, _p, b in app.calls]
        self.assertIn({"hook": "run_shortcut", "shortcut_id": "lights"}, bodies)
        self.assertIn({"hook": "run_action", "row_id": "front", "action_id": "snap", "mode": "guest"}, bodies)

    def test_failing_app_is_skipped(self):
        with mock.patch("broker.remote.requests.request", return_value=mock.Mock(status_code=500)):
            data = status.home_data("guest")
        self.assertNotIn("cams", data["status"])

    def test_dynamic_setting_options(self):
        app = FakeApp(self.install, {"setting_options": [["a", "Kitchen"], ["b", "Deck"]]})
        speaker = next(s for s in self.spec().settings if s.key == "speaker")
        with mock.patch("broker.remote.requests.request", side_effect=app):
            self.assertEqual(speaker.options, (("a", "Kitchen"), ("b", "Deck")))
        self.assertEqual(app.calls[0][2], {"hook": "setting_options", "key": "speaker"})


class ManifestFetchTests(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)

    def test_fetch_saves_manifest_and_requested_permissions(self):
        install = make_install(manifest={"id": "cams"}, requested=[])
        install.manifest = None
        install.save()
        app = FakeApp(install, {"manifest": manifest(permissions=["alerts", "speakers"], memory_mb=128)})
        with mock.patch("broker.remote.requests.request", side_effect=app):
            got = remote.fetch_manifest(install)
        self.assertEqual(got["id"], "cams")
        install.refresh_from_db()
        self.assertEqual(install.requested_permissions, ["alerts", "speakers"])
        self.assertEqual(install.memory_mb, 128)
        self.assertIsNotNone(install.manifest_fetched_at)

    def test_wrong_id_rejected(self):
        install = make_install()
        app = FakeApp(install, {"manifest": manifest(id="other")})
        with mock.patch("broker.remote.requests.request", side_effect=app):
            with self.assertRaises(remote.RemoteError):
                remote.fetch_manifest(install)


class OriginTests(TestCase):
    def test_origins(self):
        self.assertEqual(remote.hub_origin("hub.local:8000"), "http://hub.local:8000")
        self.assertEqual(remote.app_origin(9101, "hub.local:8000"), "http://hub.local:9101")
        self.assertEqual(remote.app_origin(9101, "[::1]:8000"), "http://[::1]:9101")
        self.assertIsNone(remote.page_url("cams", "//evil/"))
        self.assertEqual(remote.raw_url("cams", "javascript:x"), "")
