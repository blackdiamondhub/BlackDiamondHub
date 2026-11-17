"""Container apps: the supervisor client, the catalog, install/upgrade/uninstall
and keeping apps running from the proxy."""
import asyncio
import io
import json
import tempfile
import time
from unittest.mock import patch

import httpx
from asgiref.sync import async_to_sync
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from broker import catalog, lifecycle, proxy, remote, services, supervisor
from broker.models import AppInstall
from broker.tokens import token_for

from .base import make_install, reset_broker
from .test_proxy import FakeUpstream, ProxyTestCase, response, run, scope

SUPERVISOR = {"BDH_SUPERVISOR_SOCKET": "/run/test.sock", "BDH_SUPERVISOR_TOKEN": "t" * 64}


def version(v="1.0.0", **kw):
    out = {"version": v, "kind": "image", "image": f"ghcr.io/blackdiamondlodge/bdh-app-hello@sha256:{'0' * 64}",
           "wheel_url": f"https://x/app-{v}.whl", "sha256": "0" * 64, "hub_api": 2, "requires_sdk": "", "permissions": ["alerts"], "network": "none", "lifecycle": "on_demand",
           "memory_mb": 128, "companions": [], "provides": [], "changelog": ""}
    out.update(kw)
    return out


def catalog_data(*versions, app_id="hello", schema=1):
    return {"schema": schema, "apps": {app_id: {"name": "Hello", "description": "", "repo": "x/y",
                                                "versions": list(versions or [version()])}}}


class FakeSupervisor:
    """Records requests and answers like bdh-supervisor."""

    def __init__(self):
        self.requests = []
        self.apps = {}
        self.status = "installed"

    def view(self, app_id):
        rec = self.apps[app_id]
        return {"app_id": app_id, "status": self.status, "desired": "running", "installed_version": rec["version"],
                "error": "boom" if self.status == "failed" else "", "network": rec["network"],
                "lifecycle": "on_demand", "memory_mb": 128, "upstream": f"http://10.213.{rec['slot']}.2:8000"}

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("authorization") != "Bearer " + "t" * 64:
            return httpx.Response(401, json={"detail": "bad token"})
        path, method = request.url.path, request.method
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True, "prepared": True, "hub_api": 2, "apps": len(self.apps)})
        if path == "/apps":
            return httpx.Response(200, json={"apps": [self.view(a) for a in self.apps]})
        parts = path.strip("/").split("/")
        app_id = parts[1]
        if method == "PUT":
            body = json.loads(request.content)
            self.apps[app_id] = body
            return httpx.Response(202, json=self.view(app_id))
        if app_id not in self.apps:
            return httpx.Response(404, json={"detail": f"unknown app {app_id}"})
        if method == "DELETE":
            del self.apps[app_id]
            return httpx.Response(200, json={"deleted": app_id,
                                             "purged_data": request.url.params["purge_data"] == "true"})
        if len(parts) == 3 and parts[2] == "start":
            return httpx.Response(200, json={**self.view(app_id), "healthy": True})
        if len(parts) == 3 and parts[2] == "logs":
            return httpx.Response(200, text="line1\nline2\n")
        return httpx.Response(200, json=self.view(app_id))


class SupervisorMixin:
    def setUp(self):
        super().setUp()
        self.sup = FakeSupervisor()
        patcher = patch.object(supervisor, "transport", httpx.MockTransport(self.sup))
        patcher.start()
        self.addCleanup(patcher.stop)


@override_settings(**SUPERVISOR)
class ClientTests(SupervisorMixin, SimpleTestCase):
    def test_disabled_without_settings(self):
        with self.settings(BDH_SUPERVISOR_TOKEN=""):
            self.assertFalse(supervisor.enabled())
            with self.assertRaises(supervisor.SupervisorDisabled):
                supervisor.healthz()

    def test_sends_the_token_and_maps_errors(self):
        self.assertTrue(supervisor.healthz()["ok"])
        with self.assertRaisesMessage(supervisor.NotFound, "unknown app nope"):
            supervisor.get_app("nope")
        with self.settings(BDH_SUPERVISOR_TOKEN="x" * 64):
            with self.assertRaisesMessage(supervisor.SupervisorError, "refused the token") as ctx:
                supervisor.healthz()
            self.assertNotIsInstance(ctx.exception, supervisor.NotFound)

    def test_validation_errors_and_unreachable(self):
        def invalid(request):
            return httpx.Response(422, json={"detail": [{"msg": "slot too big"}]})

        with patch.object(supervisor, "transport", httpx.MockTransport(invalid)):
            with self.assertRaisesMessage(supervisor.Invalid, "slot too big"):
                supervisor.put_app("a", version="1", token="0" * 64, slot=999, network="none")

        def down(request):
            raise httpx.ConnectError("no such file")

        with patch.object(supervisor, "transport", httpx.MockTransport(down)):
            with self.assertRaises(supervisor.SupervisorUnavailable):
                supervisor.healthz()

    def test_put_body_and_logs(self):
        supervisor.put_app("hello", version="1.0.0", token="a" * 64, slot=3, network="outbound",
                           allow_lan=True, time_zone="America/Vancouver", hub_brand={"name": "Hub"})
        self.assertEqual(json.loads(self.sup.requests[-1].content),
                         {"version": "1.0.0", "token": "a" * 64, "slot": 3, "network": "outbound",
                          "allow_lan": True, "time_zone": "America/Vancouver", "hub_brand": {"name": "Hub"}})
        self.assertEqual(supervisor.logs("hello", lines=5), "line1\nline2\n")
        self.assertEqual(self.sup.requests[-1].url.params["lines"], "5")


class CatalogTestCase(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = self.settings(BDH_CATALOG_CACHE_DIR=tmp.name, BDH_CATALOG_URL="https://cat/catalog.json")
        override.enable()
        self.addCleanup(override.disable)
        catalog.reset()
        self.addCleanup(catalog.reset)
        self.served = catalog_data()
        self.fetches = []
        patcher = patch.object(catalog, "transport", httpx.MockTransport(self.serve))
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, request):
        self.fetches.append(request)
        if isinstance(self.served, Exception):
            raise self.served
        if isinstance(self.served, httpx.Response):
            return self.served
        # The first catalog served is "v1"; anything else gets its own tag.
        tag = '"v1"' if self.served == catalog_data() else f'"{hash(json.dumps(self.served, sort_keys=True))}"'
        if request.headers.get("if-none-match") == tag:
            return httpx.Response(304)
        return httpx.Response(200, json=self.served, headers={"etag": tag})


class CatalogTests(CatalogTestCase):
    def test_fetches_caches_and_revalidates(self):
        self.assertEqual(catalog.refresh(), "updated")
        self.assertEqual(catalog.refresh(), "fresh")
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(catalog.refresh(force=True), "unchanged")
        self.assertEqual(self.fetches[-1].headers["if-none-match"], '"v1"')
        catalog.reset()  # a restart reads the disk copy
        self.assertEqual(catalog.get(refresh_stale=False)["apps"]["hello"]["name"], "Hello")

    def test_failure_keeps_the_last_good_copy(self):
        catalog.refresh()
        for bad in (httpx.ConnectError("down"), httpx.Response(500), httpx.Response(200, json={"schema": 2, "apps": {}}),
                    httpx.Response(200, content=b"not json")):
            self.served = bad
            with self.assertRaises(catalog.CatalogError):
                catalog.refresh(force=True)
            self.assertIn("hello", catalog.get(refresh_stale=False)["apps"])
        # and it doesn't retry on every call
        count = len(self.fetches)
        catalog.get()
        self.assertEqual(len(self.fetches), count)

    def test_get_cached_never_waits_on_a_stale_catalog(self):
        data, thread = catalog.get_cached()  # nothing cached yet: the one blocking fetch
        self.assertIsNone(thread)
        self.assertEqual((data["apps"]["hello"]["name"], len(self.fetches)), ("Hello", 1))
        self.assertEqual(catalog.get_cached(), (data, None))  # fresh: no fetch
        self.assertEqual(len(self.fetches), 1)
        catalog._cache["fetched"] = time.time() - catalog.MAX_AGE - 1
        self.served = catalog_data(version("2.0.0"))
        data, thread = catalog.get_cached()
        self.assertEqual(data["apps"]["hello"]["versions"][0]["version"], "1.0.0")  # the old copy, right away
        thread.join(5)
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(catalog.get_cached()[0]["apps"]["hello"]["versions"][0]["version"], "2.0.0")

    def test_find_refetches_once_for_a_version_published_since_the_last_fetch(self):
        catalog.refresh()
        self.served = catalog_data(version("2.0.0"), version("1.0.0"))
        self.assertEqual(catalog.find("hello", "2.0.0")["version"], "2.0.0")
        count = len(self.fetches)
        self.assertEqual(catalog.find("hello", "1.0.0")["version"], "1.0.0")  # listed: no refetch
        with self.assertRaisesMessage(catalog.CatalogError, "no version 9"):
            catalog.find("hello", "9")
        self.assertEqual(len(self.fetches), count + 1)

    def test_rejects_unknown_schema(self):
        self.served = catalog_data(schema=2)
        with self.assertRaisesMessage(catalog.CatalogError, "unknown catalog schema 2"):
            catalog.refresh()
        self.assertIsNone(catalog.get(refresh_stale=False))

    def test_filters_versions_the_hub_cannot_use(self):
        data = catalog_data(version("4.0.0", hub_api=99), version("3.0.0", yanked=True),
                            version("2.0.0", requires_sdk=">=999"), version("1.5.0", requires_sdk=">=0.1"),
                            version("1.2.0", kind="wheel"), version("1.1.0", image=""), version("1.0.0", hub_api=1))
        self.assertEqual([v["version"] for v in catalog.versions("hello", data)], ["1.5.0", "1.0.0"])
        self.assertEqual(catalog.compatible(version(kind="wheel")), (False, "not an image"))
        self.assertEqual(catalog.find("hello", data=data)["version"], "1.5.0")
        with self.assertRaisesMessage(catalog.CatalogError, "yanked"):
            catalog.find("hello", "3.0.0", data=data)
        with self.assertRaisesMessage(catalog.CatalogError, "no version 9"):
            catalog.find("hello", "9", data=data)
        with self.assertRaisesMessage(catalog.CatalogError, "isn't in the catalog"):
            catalog.find("nope", data=data)
        only_new = catalog_data(version("4.0.0", hub_api=99))
        self.assertEqual(catalog.apps(only_new), {})
        self.assertEqual(catalog.compatible(version(requires_sdk="nonsense"))[0], False)


@override_settings(**SUPERVISOR)
class ServiceTests(SupervisorMixin, CatalogTestCase, TestCase):
    def setUp(self):
        super().setUp()
        reset_broker()
        self.addCleanup(reset_broker)
        patcher = patch.object(services, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manifests = []
        patcher = patch("broker.remote.fetch_manifest", side_effect=self.manifests.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, *versions, **kw):
        self.served = catalog_data(*versions)
        catalog.refresh(force=True)
        return services.install("hello", **kw)

    def test_install_puts_to_the_supervisor_and_records_the_approval(self):
        record = self.install(version(network="outbound"), allow_lan=True)
        put = next(r for r in self.sup.requests if r.method == "PUT")
        body = json.loads(put.content)
        self.assertEqual(record.slot, record.port - 9100)
        self.assertEqual(body["slot"], record.slot)
        self.assertEqual(body["token"], token_for(record))
        self.assertEqual((body["version"], body["network"], body["allow_lan"]), ("1.0.0", "outbound", True))
        record.refresh_from_db()
        self.assertTrue(record.is_container and record.enabled)
        self.assertEqual(record.upstream, f"http://10.213.{record.slot}.2:8000")
        self.assertEqual((record.granted_permissions, record.approved_network), (["alerts"], "outbound"))
        self.assertEqual(self.manifests, [record])
        self.assertTrue(any(r.url.path == "/apps/hello/start" for r in self.sup.requests))

    def test_network_can_be_narrowed(self):
        record = self.install(version(network="lan"), network="none")
        self.assertEqual(record.approved_network, "none")
        self.assertEqual(self.sup.apps["hello"]["network"], "none")

    def test_rejects_hub_api_1_local_apps_and_disabled_supervisor(self):
        with self.assertRaisesMessage(services.ServiceError, "hub API 1 app"):
            self.install(version(hub_api=1))
        with self.settings(HUB_PLUGINS=[type("S", (), {"id": "hello"})()]):
            with self.assertRaisesMessage(services.ServiceError, "runs in-process"):
                self.install()
        with self.settings(BDH_SUPERVISOR_SOCKET=""):
            with self.assertRaises(supervisor.SupervisorDisabled):
                self.install()
        self.assertFalse(AppInstall.objects.exists())

    def test_failed_install(self):
        self.sup.status = "failed"
        with self.assertRaisesMessage(services.ServiceError, "install failed: boom"):
            self.install()

    def test_put_failure_removes_the_record(self):
        self.served = catalog_data()
        with patch.object(supervisor, "transport", httpx.MockTransport(lambda r: httpx.Response(502, json={"detail": "docker"}))):
            with self.assertRaisesMessage(supervisor.SupervisorError, "docker"):
                services.install("hello")
        self.assertFalse(AppInstall.objects.exists())

    def test_update_that_widens_needs_reapproval(self):
        self.install(version())
        self.served = catalog_data(version("2.0.0", permissions=["alerts", "ha.call:light.turn_on"],
                                           network="outbound"), version())
        catalog.refresh(force=True)
        with self.assertRaises(services.ReapprovalRequired) as ctx:
            services.install("hello")
        self.assertEqual(ctx.exception.widened, {"permissions": ["ha.call:light.turn_on"], "network": "outbound"})
        self.assertEqual(AppInstall.objects.get().version, "1.0.0")
        record = services.install("hello", approve=True)
        self.assertEqual((record.version, record.approved_network), ("2.0.0", "outbound"))
        self.assertEqual(record.granted_permissions, ["alerts", "ha.call:light.turn_on"])

    def test_update_that_asks_for_less_needs_no_approval(self):
        self.install(version(permissions=["alerts", "x"]))
        old = {"id": "hello", "version": "1.0.0"}
        AppInstall.objects.update(manifest=old)
        self.served = catalog_data(version("2.0.0", permissions=["alerts"]))
        catalog.refresh(force=True)
        record = services.install("hello")
        record.refresh_from_db()
        # The old manifest keeps the app's page up until the new one is fetched.
        self.assertEqual((record.version, record.manifest), ("2.0.0", old))
        self.assertFalse(record.manifest_current)
        self.assertEqual(self.sup.apps["hello"]["version"], "2.0.0")

    def test_manifest_fetch_retries_while_the_app_starts(self):
        record = self.install()
        self.manifests.clear()
        calls = []

        def flaky(install):
            calls.append(install)
            if len(calls) < 3:
                raise remote.RemoteError("connection refused")

        with patch("broker.remote.fetch_manifest", side_effect=flaky):
            self.assertTrue(services._load_manifest(record))
        self.assertEqual(len(calls), 3)

    def test_manifest_fetch_gives_up(self):
        record = self.install()
        with patch("broker.remote.fetch_manifest", side_effect=remote.RemoteError("down")) as fetch:
            self.assertFalse(services._load_manifest(record, tries=4))
        self.assertEqual(fetch.call_count, 4)

    def test_rotate_token(self):
        record = self.install()
        old = self.sup.apps["hello"]["token"]
        services.rotate_token("hello")
        record.refresh_from_db()
        self.assertEqual(record.generation, 2)
        self.assertNotEqual(self.sup.apps["hello"]["token"], old)
        self.assertEqual(self.sup.apps["hello"]["token"], token_for(record))

    def test_uninstall(self):
        self.install()
        self.assertEqual(services.uninstall("hello", purge=True), {"deleted": "hello", "purged_data": True})
        self.assertFalse(AppInstall.objects.exists())
        self.assertEqual(self.sup.apps, {})
        with self.assertRaisesMessage(services.ServiceError, "isn't installed"):
            services.uninstall("hello")

    def test_commands(self):
        self.served = catalog_data()
        out = io.StringIO()
        call_command("catalog_refresh", stdout=out)
        self.assertIn("hello", out.getvalue())
        out = io.StringIO()
        call_command("app_install", "hello", "--version", "1.0.0", "--network", "none", stdout=out)
        self.assertIn("hello 1.0.0", out.getvalue())
        out = io.StringIO()
        call_command("app_status", "--json", stdout=out)
        status = json.loads(out.getvalue())
        self.assertEqual(status["apps"][0]["supervisor"]["status"], "installed")
        self.assertEqual(status["orphans"], [])
        out = io.StringIO()
        call_command("app_status", stdout=out)
        self.assertIn("installed/running", out.getvalue())
        self.served = catalog_data(version("2.0.0", permissions=["alerts", "more"]))
        catalog.refresh(force=True)
        with self.assertRaisesMessage(CommandError, "--approve"):
            call_command("app_install", "hello")
        call_command("app_uninstall", "hello", "--purge", stdout=io.StringIO())
        self.assertFalse(AppInstall.objects.exists())

    def test_declined_permissions_stay_off_without_reapproval(self):
        record = self.install(version(permissions=["alerts", "speakers"]), permissions=["alerts"])
        self.assertEqual(record.granted_permissions, ["alerts"])
        self.assertEqual(services.declined(record), {"speakers"})
        self.served = catalog_data(version("2.0.0", permissions=["alerts", "speakers"]))
        catalog.refresh(force=True)
        record = services.install("hello")
        self.assertEqual((record.version, record.granted_permissions), ("2.0.0", ["alerts"]))
        services.set_permissions("hello", ["alerts", "speakers", "not-asked"])
        record.refresh_from_db()
        self.assertEqual(record.granted_permissions, ["alerts", "speakers"])

    def test_update_all_skips_updates_that_need_approval(self):
        self.install(version())
        self.assertIsNone(services.update_available(AppInstall.objects.get()))
        self.served = catalog_data(version("1.1.0"), version("1.0.0"))
        catalog.refresh(force=True)
        record = AppInstall.objects.get()
        self.assertEqual(services.update_available(record)["version"], "1.1.0")
        record.auto_update = False
        record.save()
        self.assertEqual(services.update_all(only_auto=True), {})
        self.assertEqual(services.update_all(), {"hello": "updated 1.1.0"})
        self.served = catalog_data(version("1.2.0", permissions=["alerts", "speakers"]), version("1.1.0"))
        catalog.refresh(force=True)
        self.assertEqual(services.update_all(), {"hello": "needs approval"})
        self.assertEqual(AppInstall.objects.get().version, "1.1.0")

    def test_newer_compares_numerically(self):
        self.assertTrue(services._newer("1.10.0", "1.9.0"))
        self.assertFalse(services._newer("1.9.0", "1.10.0"))
        self.assertFalse(services._newer("1.0.0", "1.0.0"))

    def test_autoupdate_command(self):
        from broker.management.commands import app_autoupdate

        self.install(version())
        self.served = catalog_data(version("1.1.0"), version("1.0.0"))
        catalog.refresh(force=True)
        out = io.StringIO()
        with patch.object(app_autoupdate, "local_hour", return_value=12):
            call_command("app_autoupdate", "--nightly", stdout=out)
        self.assertEqual((out.getvalue(), AppInstall.objects.get().version), ("", "1.0.0"))
        with patch.object(app_autoupdate, "local_hour", return_value=app_autoupdate.NIGHT_HOUR):
            call_command("app_autoupdate", "--nightly", stdout=out)
        self.assertIn("hello: updated 1.1.0", out.getvalue())
        self.assertEqual(AppInstall.objects.get().version, "1.1.0")

    def test_autoupdate_sees_a_release_newer_than_the_fresh_cache(self):
        self.install(version())
        catalog.refresh(force=True)
        self.served = catalog_data(version("1.1.0"), version("1.0.0"))
        out = io.StringIO()
        call_command("app_autoupdate", stdout=out)
        self.assertIn("hello: updated 1.1.0", out.getvalue())

    def test_site_install_adds_missing_container_apps(self):
        self.served = catalog_data(version(network="lan"))
        catalog.refresh(force=True)
        with self.settings(HUB_CONTAINER_APPS=["hello", "gone"],
                           HUB_SITE_APPS={"hello": {"runtime": "container", "allow_lan": True}}):
            out = io.StringIO()
            call_command("app_siteinstall", stdout=out)
            self.assertIn("hello: installed 1.0.0", out.getvalue())
            self.assertIn("gone: failed:", out.getvalue())
            record = AppInstall.objects.get()
            self.assertEqual((record.app_id, record.approved_network, record.allow_lan), ("hello", "lan", True))
            puts = sum(r.method == "PUT" for r in self.sup.requests)
            self.assertEqual(list(services.install_site_apps()), ["gone"])
            self.assertEqual(sum(r.method == "PUT" for r in self.sup.requests), puts)

    def test_site_install_without_supervisor(self):
        out = io.StringIO()
        with self.settings(BDH_SUPERVISOR_SOCKET="", HUB_CONTAINER_APPS=["hello"]):
            call_command("app_siteinstall", stdout=out)
        self.assertIn("supervisor not configured", out.getvalue())
        self.assertFalse(AppInstall.objects.exists())

    def test_status_without_supervisor(self):
        out = io.StringIO()
        with self.settings(BDH_SUPERVISOR_SOCKET=""):
            call_command("app_status", stdout=out)
        self.assertIn("supervisor: not configured", out.getvalue())


@override_settings(**SUPERVISOR)
class ContainerProxyTests(ProxyTestCase):
    def setUp(self):
        super().setUp()
        lifecycle.reset()
        self.addCleanup(lifecycle.reset)
        self.install.runtime = AppInstall.CONTAINER
        self.install.save()
        reset_broker()
        self.starts, self.activity = [], []
        self.start_delay = 0
        patcher = patch.object(supervisor, "astart", self.astart)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(supervisor, "aactivity", self.aactivity)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def astart(self, app_id, wait=True):
        self.starts.append(app_id)
        await asyncio.sleep(self.start_delay)
        return {"healthy": True}

    async def aactivity(self, app_id):
        self.activity.append(app_id)
        return {"running": True}

    def get(self):
        return response(run(proxy.app_proxy, scope(self.port, "/")))

    def test_starts_the_app_then_proxies(self):
        self.assertEqual(self.get()[0], 200)
        self.assertEqual(self.get()[0], 200)
        self.assertEqual(self.starts, ["cams"])  # trusted as running for a minute
        self.assertEqual(len(self.upstream.requests), 2)

    def test_slow_start_shows_the_starting_page(self):
        self.start_delay = 0.5
        with patch.object(lifecycle, "HOLD", 0.05):
            status, headers, body = self.get()
        self.assertEqual(status, 503)
        self.assertIn(b"Cameras is starting", body)
        self.assertEqual(headers["retry-after"], ["3"])
        self.assertEqual(self.upstream.requests, [])

    def test_an_app_calling_the_broker_skips_the_start_check(self):
        lifecycle.seen(self.install)
        self.start_delay = 0.5
        with patch.object(lifecycle, "HOLD", 0.05):
            self.assertEqual(self.get()[0], 200)
        self.assertEqual(self.starts, [])

    def test_failed_start_lets_the_request_through_and_backs_off(self):
        async def broken(app_id, wait=True):
            self.starts.append(app_id)
            raise supervisor.SupervisorUnavailable("down")

        self.upstream.error = httpx.ConnectError("refused")
        with patch.object(supervisor, "astart", broken):
            self.assertEqual(self.get()[0], 503)
            self.get()
        self.assertEqual(self.starts, ["cams"])

    def test_activity_is_throttled(self):
        async def go():
            for _ in range(5):
                lifecycle.touch(self.install)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        async_to_sync(go)()
        self.assertEqual(self.activity, ["cams"])
        lifecycle._last_activity["cams"] -= lifecycle.ACTIVITY_EVERY
        async_to_sync(go)()
        self.assertEqual(self.activity, ["cams", "cams"])

    def test_activity_saying_stopped_forgets_running(self):
        async def stopped(app_id):
            return {"running": False}

        async def go():
            await lifecycle.ensure_running(self.install)
            lifecycle._last_activity.clear()
            with patch.object(supervisor, "aactivity", stopped):
                lifecycle.touch(self.install)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        async_to_sync(go)()
        self.assertNotIn("cams", lifecycle._running_until)

    def test_external_apps_are_left_alone(self):
        self.install.runtime = AppInstall.EXTERNAL
        self.install.save()
        reset_broker()
        self.assertEqual(self.get()[0], 200)
        self.assertEqual((self.starts, self.activity), ([], []))
