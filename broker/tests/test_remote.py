import copy
import json
import time
from unittest import mock

import requests
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


class SleepingAppTests(TestCase):
    """On-demand apps stay asleep for tile polls and show their last results."""

    def setUp(self):
        reset_broker()
        cache.clear()
        self.addCleanup(reset_broker)
        self.install = make_install(runtime="container", lifecycle="on_demand",
                                    manifest=manifest(wake_every=3600))
        self.app = FakeApp(self.install, {"status": STATUS, "shortcuts": SHORTCUTS, "run_shortcut": "Done"})
        self.awake = True
        self.starts = 0
        patches = [
            mock.patch("broker.remote.requests.request", side_effect=self.request),
            mock.patch("broker.supervisor.enabled", return_value=True),
            mock.patch("broker.supervisor.start", side_effect=self.start),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def request(self, *args, **kwargs):
        if not self.awake:
            raise requests.ConnectionError("refused")
        return self.app(*args, **kwargs)

    def start(self, app_id, wait=False):
        self.starts += 1
        self.awake = True
        return {"healthy": True}

    def poll(self):
        cache.delete(status._fresh_key("status", "cams", "guest"))
        return status.home_data("guest")["status"].get("cams")

    def test_asleep_serves_last_result_without_waking(self):
        self.poll()
        self.awake = False
        got = self.poll()
        self.assertEqual(got["text"], "2 cameras")
        self.assertFalse(got["stale"])
        self.assertEqual(self.starts, 0)

    def test_wakes_once_last_result_is_older_than_wake_every(self):
        self.poll()
        self.awake = False
        key = status._last_key("status", "cams", "guest")
        cache.set(key, {**cache.get(key), "updated": time.time() - 3601})
        got = self.poll()
        self.assertEqual(self.starts, 1)
        self.assertFalse(got["stale"])

    def test_wakes_when_nothing_cached(self):
        self.awake = False
        self.assertEqual(self.poll()["text"], "2 cameras")
        self.assertGreaterEqual(self.starts, 1)

    def test_last_result_outlives_wake_every(self):
        hook = status._resolve("remote:cams:status")
        self.assertEqual(status._keep_for(hook), status.LAST_GOOD_TTL)
        self.install.manifest = manifest(wake_every=12 * 3600)
        self.install.save()
        self.assertEqual(status._keep_for(hook), 13 * 3600)
        self.install.lifecycle = "always"
        self.install.save()
        self.assertEqual(status._keep_for(hook), status.LAST_GOOD_TTL)

    def test_shortcut_wakes_it(self):
        self.awake = False
        self.assertEqual(status.run_shortcut("cams", "lights", "guest"), "Done")
        self.assertEqual(self.starts, 1)

    def test_always_on_app_is_still_woken(self):
        self.install.lifecycle = "always"
        self.install.save()
        remote.installs_changed()
        self.awake = False
        self.assertEqual(self.poll()["text"], "2 cameras")
        self.assertGreaterEqual(self.starts, 1)

    def make_tileless(self):
        self.install.manifest = manifest(wake_every=3600, tiles=[], hooks=["status"])
        self.install.save()
        remote.installs_changed()

    def test_tileless_app_is_woken_by_the_waker(self):
        from hub import waker

        self.make_tileless()
        self.awake = False
        # The homepage never polls an app without a tile...
        self.assertIsNone(self.poll())
        self.assertEqual(self.starts, 0)
        # ...so the waker does.
        self.assertEqual(waker.tick(), ["cams"])
        self.assertEqual(self.starts, 1)

    def test_waker_lets_tileless_app_sleep_until_wake_every(self):
        from hub import waker

        self.make_tileless()
        waker.tick()
        self.awake = False
        cache.delete(status._fresh_key("status", "cams"))
        waker.tick()
        self.assertEqual(self.starts, 0)
        key = status._last_key("status", "cams")
        cache.set(key, {**cache.get(key), "updated": time.time() - 3601})
        cache.delete(status._fresh_key("status", "cams"))
        waker.tick()
        self.assertEqual(self.starts, 1)

    def test_waker_skips_apps_with_tiles(self):
        from hub import waker

        self.awake = False
        self.assertEqual(waker.tick(), [])
        self.assertEqual(self.starts, 0)


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


@override_settings(HUB_PLUGINS=())
class StaleManifestTests(TestCase):
    """After an upgrade the old manifest keeps serving until the new one arrives."""

    def setUp(self):
        reset_broker()
        cache.clear()
        self.addCleanup(reset_broker)
        self.install = make_install(version="2.0.0", manifest=manifest(version="1.0.0"))

    def test_stale_manifest_still_serves_and_is_refetched(self):
        app = FakeApp(self.install, {"manifest": manifest(version="2.0.0", name="Cams 2")})
        with mock.patch("broker.remote.requests.request", side_effect=app):
            spec = remote.spec_for("cams")
        self.assertIsNotNone(spec)
        self.assertEqual([c[1] for c in app.calls], ["/_bdh/manifest"])
        self.install.refresh_from_db()
        self.assertTrue(self.install.manifest_current)

    def test_failed_refetch_keeps_old_spec_and_retries_sooner(self):
        with mock.patch("broker.remote.requests.request", side_effect=requests.ConnectionError("refused")) as req:
            self.assertIsNotNone(remote.spec_for("cams"))
            self.assertIsNotNone(remote.spec_for("cams"))
            self.assertEqual(req.call_count, 1)  # cached, and gated per app
            later = time.monotonic() + remote.STALE_RETRY + 1
            with mock.patch("broker.remote.time.monotonic", return_value=later):
                self.assertIsNotNone(remote.spec_for("cams"))
            self.assertEqual(req.call_count, 2)
        self.install.refresh_from_db()
        self.assertEqual(self.install.manifest["version"], "1.0.0")

    def test_current_manifest_is_not_refetched(self):
        self.install.manifest = manifest(version="2.0.0")
        self.install.save()
        with mock.patch("broker.remote.requests.request") as req:
            later = time.monotonic() + remote.STALE_RETRY + 1
            self.assertIsNotNone(remote.spec_for("cams"))
            with mock.patch("broker.remote.time.monotonic", return_value=later):
                self.assertIsNotNone(remote.spec_for("cams"))
        req.assert_not_called()


class OriginTests(TestCase):
    def test_origins(self):
        self.assertEqual(remote.hub_origin("hub.local:8000"), "http://hub.local:8000")
        self.assertEqual(remote.app_origin(9101, "hub.local:8000"), "http://hub.local:9101")
        self.assertEqual(remote.app_origin(9101, "[::1]:8000"), "http://[::1]:9101")
        self.assertIsNone(remote.page_url("cams", "//evil/"))
        self.assertEqual(remote.raw_url("cams", "javascript:x"), "")
