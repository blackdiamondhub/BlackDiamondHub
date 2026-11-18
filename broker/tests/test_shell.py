from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase

from .base import make_install, manifest, reset_broker


class ShellPageTests(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        self.install = make_install()

    def test_iframe_points_at_app_origin(self):
        resp = self.client.get("/apps/cams/cameras/live?cam=2")
        self.assertEqual(resp.status_code, 200)
        src = f"http://testserver:{self.install.port}/cameras/live?cam=2"
        self.assertContains(resp, f'src="{src}"')
        self.assertContains(resp, f'data-app-origin="http://testserver:{self.install.port}"')
        self.assertContains(resp, 'sandbox="allow-scripts allow-same-origin allow-forms allow-popups"')
        self.assertContains(resp, "broker/embed-host.js")
        self.assertContains(resp, "localStorage.getItem('bdh-app-bg:cams')")
        self.assertContains(resp, "#bdh-app-frame:fullscreen")

    def test_root(self):
        resp = self.client.get("/apps/cams/")
        self.assertContains(resp, f'src="http://testserver:{self.install.port}/"')

    def test_unknown_or_disabled_app(self):
        self.assertEqual(self.client.get("/apps/nope/").status_code, 404)
        self.install.enabled = False
        self.install.save()
        self.assertEqual(self.client.get("/apps/cams/").status_code, 404)

    def test_app_without_manifest_yet_gets_a_self_refreshing_page(self):
        self.install.manifest = None
        self.install.save()
        with mock.patch("broker.remote.requests.request", side_effect=requests.ConnectionError("refused")):
            for url in ("/apps/cams/", "/apps/cams/_bare/cameras/popup/1"):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 503, url)
                self.assertEqual(resp["Retry-After"], "5")
                self.assertContains(resp, '<meta http-equiv="refresh" content="5">', status_code=503)
            self.assertEqual(self.client.get("/apps/cams/_raw/static/x.png").status_code, 404)

    def test_stale_manifest_keeps_the_page_up(self):
        self.install.version = "9.9.9"
        self.install.save()
        with mock.patch("broker.remote.requests.request", side_effect=requests.ConnectionError("refused")):
            self.assertEqual(self.client.get("/apps/cams/").status_code, 200)

    def test_internal_paths(self):
        self.assertEqual(self.client.get("/apps/cams/_bdh/manifest").status_code, 404)
        self.assertEqual(self.client.get("/apps/cams/_bare/_bdh/hook").status_code, 404)

    def test_bare_page(self):
        resp = self.client.get("/apps/cams/_bare/cameras/popup/1")
        self.assertContains(resp, f"http://testserver:{self.install.port}/cameras/popup/1")
        self.assertContains(resp, "<title>Cameras</title>")
        self.assertContains(resp, 'title="Cameras"')
        self.assertContains(resp, "#bdh-app-frame:fullscreen")
        self.assertNotContains(resp, "bdh-app-bg:")
        self.assertNotContains(resp, "html, body, .content-wrapper, #bdh-app-frame { background: #000 !important; }")

    def test_x_frame_options_sameorigin(self):
        resp = self.client.get("/apps/cams/")
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")

    def test_navigate(self):
        resp = self.client.get("/apps/_nav/", {"name": "broker:shell", "args": '["cams"]'})
        self.assertRedirects(resp, "/apps/cams/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/apps/_nav/", {"name": "nope"}).status_code, 404)

    def test_navigate_only_to_home_and_app_pages(self):
        resp = self.client.get("/apps/_nav/", {"name": "broker:shell_path", "args": '["cams", "live/1"]'})
        self.assertRedirects(resp, "/apps/cams/live/1", fetch_redirect_response=False)
        resp = self.client.get("/apps/_nav/", {"name": "landing_page"})
        self.assertRedirects(resp, "/", fetch_redirect_response=False)
        # An app's back link to its own settings page (the shell checks the id is its own).
        resp = self.client.get("/apps/_nav/", {"name": "hub_settings_app", "args": '["cams"]'})
        self.assertRedirects(resp, "/settings/app/cams/", fetch_redirect_response=False)
        for name, args in (("admin:index", "[]"), ("logout", "[]"), ("display_settings", "[]"),
                           ("hub_settings:index", "[]"), ("broker:raw", '["cams", "x.png"]'),
                           ("broker:bare_root", '["cams"]'), ("broker:shell", '["_nav"]'),
                           ("broker:shell_path", '["cams", "_raw/x.png"]')):
            resp = self.client.get("/apps/_nav/", {"name": name, "args": args})
            self.assertEqual(resp.status_code, 404, name)


class RawTests(TestCase):
    """``/apps/<app>/_raw/``: tile icons must show while an on-demand app sleeps."""

    def setUp(self):
        reset_broker()
        cache.clear()
        self.addCleanup(reset_broker)
        self.install = make_install(version="1.2.0")

    def reply(self, body=b"<svg/>"):
        resp = mock.Mock(status_code=200, content=body,
                         headers={"Content-Type": "image/svg+xml", "Content-Length": str(len(body))})
        resp.iter_content.return_value = iter([body])
        return resp

    def upstream(self, *results):
        """``requests.get`` answering the app's upstream with ``results`` in turn.

        Patching it patches every caller (the page's Home Assistant lookups too),
        so anything else just fails as if offline.
        """
        results = list(results)
        calls = []

        def get(url, *args, **kwargs):
            if not url.startswith(self.install.upstream):
                raise requests.ConnectionError("offline")
            calls.append(url)
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return mock.patch("broker.views.requests.get", side_effect=get), calls

    def test_asleep_static_wakes_app_once_then_served_from_cache(self):
        patch, calls = self.upstream(requests.ConnectionError(), self.reply())
        with patch, \
                mock.patch("broker.remote._wake", return_value=True) as wake:
            first = self.client.get("/apps/cams/_raw/static/cams/icon.svg")
            second = self.client.get("/apps/cams/_raw/static/cams/icon.svg")
        self.assertEqual((first.status_code, first.content), (200, b"<svg/>"))
        self.assertEqual((second.status_code, second.content), (200, b"<svg/>"))
        self.assertEqual(second["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(wake.call_count, 1)
        self.assertEqual(len(calls), 2)

    def test_asleep_non_static_media_does_not_wake(self):
        patch, _ = self.upstream(requests.ConnectionError())
        with patch, mock.patch("broker.remote._wake") as wake:
            resp = self.client.get("/apps/cams/_raw/cameras/snap/front.jpg")
        self.assertEqual(resp.status_code, 404)
        wake.assert_not_called()

    def test_flagged_media_wakes_a_sleeping_app(self):
        # The saver's live cam: without the wake it fell back to stock art.
        jpeg = self.reply(b"jpg")
        jpeg.headers["Content-Type"] = "image/jpeg"
        patch, calls = self.upstream(requests.ConnectionError(), jpeg)
        with patch, mock.patch("broker.remote._wake", return_value=True) as wake:
            resp = self.client.get("/apps/cams/_raw/latest/peak.jpg?t=5&_bdh_wake=1")
        self.assertEqual((resp.status_code, b"".join(resp.streaming_content)), (200, b"jpg"))
        wake.assert_called_once()
        self.assertTrue(calls[-1].endswith("/latest/peak.jpg?t=5"), calls[-1])

    def test_new_version_refetches(self):
        patch, _ = self.upstream(self.reply(b"old"), self.reply(b"new"))
        with patch:
            self.client.get("/apps/cams/_raw/static/cams/icon.svg")
            type(self.install).objects.filter(pk=self.install.pk).update(
                version="1.3.0", manifest=manifest(id="cams", version="1.3.0"))
            reset_broker()
            resp = self.client.get("/apps/cams/_raw/static/cams/icon.svg")
        self.assertEqual(resp.content, b"new")
