from django.test import TestCase

from .base import make_install, reset_broker


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

    def test_root(self):
        resp = self.client.get("/apps/cams/")
        self.assertContains(resp, f'src="http://testserver:{self.install.port}/"')

    def test_unknown_or_disabled_app(self):
        self.assertEqual(self.client.get("/apps/nope/").status_code, 404)
        self.install.enabled = False
        self.install.save()
        self.assertEqual(self.client.get("/apps/cams/").status_code, 404)

    def test_internal_paths(self):
        self.assertEqual(self.client.get("/apps/cams/_bdh/manifest").status_code, 404)
        self.assertEqual(self.client.get("/apps/cams/_bare/_bdh/hook").status_code, 404)

    def test_bare_page(self):
        resp = self.client.get("/apps/cams/_bare/cameras/popup/1")
        self.assertContains(resp, f"http://testserver:{self.install.port}/cameras/popup/1")

    def test_x_frame_options_sameorigin(self):
        resp = self.client.get("/apps/cams/")
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")

    def test_navigate(self):
        resp = self.client.get("/apps/_nav/", {"name": "broker:shell", "args": '["cams"]'})
        self.assertRedirects(resp, "/apps/cams/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/apps/_nav/", {"name": "nope"}).status_code, 404)
