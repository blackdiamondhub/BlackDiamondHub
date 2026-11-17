import base64
import hashlib
import io
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from PIL import Image

from broker import events_bridge, shares
from broker.models import AppInstall, AuditEntry, ShareBlob, ShareGrant, ShareRevision

from .base import auth, make_install, manifest, reset_broker

ICS = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
EVENTS = [{"name": "events", "type": "text/calendar", "title": "Sun Peaks events"}]


def png(color=(255, 0, 0), size=(4, 4), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ShareTestCase(TestCase):
    def setUp(self):
        reset_broker()
        self.addCleanup(reset_broker)
        self.provider = make_install("sunpeaks", manifest=manifest(id="sunpeaks", shares=EVENTS))
        self.reader = make_install("calendar", manifest=manifest(id="calendar", reads=["text/calendar"]))
        self.other = make_install("cams")
        sender = mock.patch("broker.events_bridge._send")
        self.sent = sender.start()
        self.addCleanup(sender.stop)
        pool = mock.patch("broker.events_bridge._executor")
        executor = pool.start()
        executor.return_value.submit.side_effect = lambda fn, *a: fn(*a)
        self.addCleanup(pool.stop)

    def call(self, op, payload=None, install=None):
        return self.client.post(f"/_broker/rpc/{op}", json.dumps(payload or {}), content_type="application/json",
                                SERVER_PORT="8099", HTTP_AUTHORIZATION=auth(install or self.provider))

    def ok(self, op, payload=None, install=None):
        resp = self.call(op, payload, install)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["result"]

    def refused(self, op, payload, code, install=None):
        resp = self.call(op, payload, install)
        self.assertEqual(resp.json()["error"]["code"], code, resp.content)
        return resp.json()["error"]["message"]

    def put(self, data, mime="text/calendar", install=None):
        return self.ok("shares.put_blob", {"mime": mime, "data_b64": base64.b64encode(data).decode()},
                       install)["sha256"]

    def publish(self, main=None, attachments=None, expected=None, install=None):
        main = main or self.put(ICS, install=install)
        with self.captureOnCommitCallbacks(execute=True):
            return self.ok("shares.publish", {"name": "events", "main": main, "attachments": attachments or {},
                                              "expected_revision": expected}, install)["revision"]

    def grant(self, consumer="calendar"):
        ShareGrant.objects.create(consumer=consumer, provider="sunpeaks", name="events")


class BlobTests(ShareTestCase):
    def test_put_missing_and_dedupe(self):
        digest = self.put(ICS)
        self.assertEqual(digest, sha(ICS))
        self.assertEqual(self.put(ICS), digest)
        self.assertEqual(ShareBlob.objects.count(), 1)
        other = "0" * 64
        self.assertEqual(self.ok("shares.missing", {"sha256": [digest, other]}), {"missing": [other]})
        # Blobs are per owner: another app still has to upload the same bytes.
        self.assertEqual(self.ok("shares.missing", {"sha256": [digest]}, self.other), {"missing": [digest]})
        self.assertEqual(self.put(ICS, install=self.other), digest)
        self.assertEqual(ShareBlob.objects.filter(sha256=digest).count(), 2)
        self.refused("shares.missing", {"sha256": ["nothex"]}, "invalid")
        self.refused("shares.missing", {"sha256": [other] * 501}, "invalid")

    def test_images_are_checked(self):
        self.assertEqual(self.put(png(), "image/png"), sha(png()))
        self.put(png(fmt="JPEG"), "image/jpeg")
        self.put(png(fmt="WEBP"), "image/webp")
        self.assertEqual(ShareBlob.objects.get(sha256=sha(png())).width, 4)
        # A PNG claiming to be a JPEG, or text claiming to be an image.
        self.refused("shares.put_blob", {"mime": "image/jpeg", "data_b64": base64.b64encode(png()).decode()},
                     "invalid")
        self.refused("shares.put_blob", {"mime": "image/png", "data_b64": base64.b64encode(ICS).decode()},
                     "invalid")
        self.refused("shares.put_blob", {"mime": "image/png", "data_b64": base64.b64encode(png()[:30]).decode()},
                     "invalid")
        self.refused("shares.put_blob", {"mime": "image/png",
                                         "data_b64": base64.b64encode(png(size=(4097, 1))).decode()}, "invalid")
        self.refused("shares.put_blob", {"mime": "text/html", "data_b64": "PGI+"}, "invalid")
        self.refused("shares.put_blob", {"mime": "text/calendar", "data_b64": base64.b64encode(b"\xff\xfe").decode()},
                     "invalid")
        self.refused("shares.put_blob", {"mime": "text/calendar", "data_b64": "!!"}, "invalid")

    def test_size_caps(self):
        with mock.patch.object(shares, "MAX_BLOB", 100):
            self.refused("shares.put_blob", {"mime": "text/calendar",
                                             "data_b64": base64.b64encode(b"x" * 101).decode()}, "too_large")
        self.refused("shares.put_blob", {"mime": "text/calendar",
                                         "data_b64": base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()},
                     "too_large")
        resp = self.client.post("/_broker/rpc/shares.put_blob", b"{" + b" " * (8 * 1024 * 1024) + b"}",
                                content_type="application/json", SERVER_PORT="8099",
                                HTTP_AUTHORIZATION=auth(self.provider))
        self.assertEqual(resp.status_code, 413)

    def test_quota(self):
        with mock.patch.object(shares, "QUOTA", len(ICS) + 10):
            self.put(ICS)
            self.refused("shares.put_blob", {"mime": "text/calendar",
                                             "data_b64": base64.b64encode(b"x" * 20).decode()}, "too_large")
            # Other apps have their own quota.
            self.put(b"x" * 20, install=self.other)

    def test_audited(self):
        self.put(ICS)
        entry = AuditEntry.objects.get(op="shares.put_blob")
        self.assertIn("text/calendar", entry.detail)
        self.assertNotIn("data_b64", entry.detail)


class PublishTests(ShareTestCase):
    def test_publish_and_open(self):
        photo = self.put(png(), "image/png")
        self.assertEqual(self.publish(attachments={"p1.png": photo}), 1)
        self.assertEqual(self.publish(expected=1), 2)
        self.grant()
        out = self.ok("shares.open", {"provider": "sunpeaks", "name": "events"}, self.reader)
        self.assertEqual(out["revision"], 2)
        self.assertEqual(base64.b64decode(out["data_b64"]), ICS)
        self.assertEqual((out["type"], out["title"], out["attachments"]),
                         ("text/calendar", "Sun Peaks events", {}))
        self.assertEqual(self.ok("shares.open", {"provider": "sunpeaks", "name": "events", "if_revision": 2},
                                 self.reader), {"revision": 2, "not_modified": True})

    def test_attachment_paths(self):
        photo = self.put(png(), "image/png")
        self.publish(attachments={"p1.png": photo})
        out = self.ok("shares.open", {"provider": "sunpeaks", "name": "events"})
        self.assertEqual(out["attachments"], {"p1.png": f"/apps/_share/{photo}"})

    def test_provider_opens_own_without_grant(self):
        self.publish()
        self.assertEqual(self.ok("shares.open", {"provider": "sunpeaks", "name": "events"})["revision"], 1)

    def test_needs_grant(self):
        self.publish()
        self.refused("shares.open", {"provider": "sunpeaks", "name": "events"}, "forbidden", self.reader)
        self.grant()
        self.ok("shares.open", {"provider": "sunpeaks", "name": "events"}, self.reader)
        ShareGrant.objects.all().delete()
        self.refused("shares.open", {"provider": "sunpeaks", "name": "events"}, "forbidden", self.reader)
        self.refused("shares.open", {"provider": "sunpeaks", "name": "events"}, "forbidden", self.other)

    def test_unpublished_or_undeclared(self):
        self.refused("shares.open", {"provider": "sunpeaks", "name": "events"}, "not_found")
        self.grant()
        self.publish()
        AppInstall.objects.filter(app_id="sunpeaks").update(manifest=manifest(id="sunpeaks"))
        self.refused("shares.open", {"provider": "sunpeaks", "name": "events"}, "not_found", self.reader)

    def test_undeclared_name(self):
        main = self.put(ICS)
        self.refused("shares.publish", {"name": "nope", "main": main}, "invalid")
        self.refused("shares.publish", {"name": "events", "main": self.put(ICS, install=self.other)},
                     "invalid", self.other)

    def test_main_must_be_own_and_typed(self):
        theirs = self.put(ICS + b"x", install=self.other)
        self.refused("shares.publish", {"name": "events", "main": theirs}, "invalid")
        photo = self.put(png(), "image/png")
        self.refused("shares.publish", {"name": "events", "main": photo}, "invalid")

    def test_attachments_must_be_own_images(self):
        main = self.put(ICS)
        theirs = self.put(png(), "image/png", install=self.other)
        self.refused("shares.publish", {"name": "events", "main": main, "attachments": {"a.png": theirs}},
                     "invalid")
        self.refused("shares.publish", {"name": "events", "main": main, "attachments": {"a.ics": main}},
                     "invalid")
        mine = self.put(png(), "image/png")
        self.refused("shares.publish", {"name": "events", "main": main, "attachments": {"../x": mine}},
                     "invalid")
        self.refused("shares.publish", {"name": "events", "main": main,
                                        "attachments": {f"a{i}": mine for i in range(201)}}, "invalid")
        self.assertFalse(ShareRevision.objects.exists())

    def test_conflict_keeps_previous_live(self):
        self.publish()
        newer = self.put(ICS + b"X-NEW:1\r\n")
        msg = self.refused("shares.publish", {"name": "events", "main": newer, "expected_revision": 0}, "invalid")
        self.assertIn("conflict", msg)
        self.refused("shares.publish", {"name": "events", "main": "f" * 64}, "invalid")
        out = self.ok("shares.open", {"provider": "sunpeaks", "name": "events"})
        self.assertEqual((out["revision"], base64.b64decode(out["data_b64"])), (1, ICS))

    def test_list(self):
        self.assertEqual(self.ok("shares.list"), {"shares": [{
            "provider": "sunpeaks", "provider_title": "Cameras", "name": "events", "title": "Sun Peaks events",
            "type": "text/calendar", "revision": 0, "updated": None}]})
        self.assertEqual(self.ok("shares.list", install=self.reader), {"shares": []})
        self.grant()
        self.publish()
        listed = self.ok("shares.list", install=self.reader)["shares"]
        self.assertEqual([(s["provider"], s["name"], s["revision"]) for s in listed], [("sunpeaks", "events", 1)])
        self.assertTrue(listed[0]["updated"])

    def test_list_names_tileless_provider_not_by_id(self):
        provider = make_install("sunpeaks_events", manifest=manifest(id="sunpeaks_events", shares=EVENTS,
                                                                     tiles=[], name=""))
        with mock.patch("broker.catalog.get", return_value={"apps": {"sunpeaks_events": {"name": "Sun Peaks Events"}}}):
            self.assertEqual(self.ok("shares.list", install=provider)["shares"][0]["provider_title"],
                             "Sun Peaks Events")
        with mock.patch("broker.catalog.get", return_value=None):
            self.assertEqual(self.ok("shares.list", install=provider)["shares"][0]["provider_title"],
                             "Sunpeaks Events")

    def test_notifies_provider_and_granted_only(self):
        self.grant()
        self.publish()
        sent = {c.args[0]: c.args[1] for c in self.sent.call_args_list}
        self.assertEqual(set(sent), {"sunpeaks", "calendar"})
        self.assertEqual(sent["calendar"]["topic"], "_shares/sunpeaks/events")
        self.assertEqual(sent["calendar"]["data"], {"provider": "sunpeaks", "name": "events", "revision": 1})

    def test_old_manifest_without_shares(self):
        AppInstall.objects.filter(app_id="sunpeaks").update(
            manifest={"id": "sunpeaks", "shares": [{"name": "Bad Name!", "type": "x", "title": ""}, "junk"],
                      "reads": "nope"})
        self.assertEqual(self.ok("shares.list"), {"shares": []})
        self.assertEqual(shares.declared(None), [])
        self.assertEqual(shares.reads({"id": "x"}), set())


class RetentionTests(ShareTestCase):
    def age(self, hours=2):
        ShareBlob.objects.update(created=timezone.now() - timedelta(hours=hours))

    def test_keeps_two_revisions_and_collects_blobs(self):
        first = self.put(ICS)
        photo = self.put(png(), "image/png")
        self.publish(first, {"p.png": photo})
        self.publish(self.put(ICS + b"2"))
        self.age()
        self.publish(self.put(ICS + b"3"))
        self.assertEqual(sorted(ShareRevision.objects.values_list("revision", flat=True)), [2, 3])
        self.assertFalse(ShareBlob.objects.filter(sha256__in=[first, photo]).exists())
        self.assertTrue(ShareBlob.objects.filter(sha256=sha(ICS + b"2")).exists())

    def test_pending_blobs_survive(self):
        self.put(png(), "image/png")
        self.assertEqual(shares.gc(), (0, 0))
        self.age()
        self.assertEqual(shares.gc(), (0, 1))

    def test_command(self):
        from django.core.management import call_command

        self.put(ICS)
        self.age()
        out = io.StringIO()
        call_command("shares_gc", stdout=out)
        self.assertIn("1 blobs", out.getvalue())

    def test_uninstall_cleans_up(self):
        self.grant()
        self.publish()
        ShareGrant.objects.create(consumer="sunpeaks", provider="cams", name="x")
        AppInstall.objects.get(app_id="sunpeaks").delete()
        self.assertFalse(ShareRevision.objects.exists())
        self.assertFalse(ShareBlob.objects.exists())
        self.assertFalse(ShareGrant.objects.exists())

    def test_consumer_uninstall_drops_grant(self):
        self.grant()
        AppInstall.objects.get(app_id="calendar").delete()
        self.assertFalse(ShareGrant.objects.exists())


class PublicBlobTests(ShareTestCase):
    def test_serves_attached_images_only(self):
        photo = self.put(png(), "image/png")
        unused = self.put(png((0, 255, 0)), "image/png")
        main = self.put(ICS)
        self.publish(main, {"p.png": photo})
        resp = self.client.get(f"/apps/_share/{photo}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, png())
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(resp["Cross-Origin-Resource-Policy"], "cross-origin")
        for digest in (main, unused, "0" * 64, "nothex"):
            self.assertEqual(self.client.get(f"/apps/_share/{digest}").status_code, 404, digest)

    def test_no_login_needed_and_not_an_app(self):
        photo = self.put(png(), "image/png")
        self.publish(attachments={"p.png": photo})
        self.client.logout()
        self.assertEqual(self.client.get(f"/apps/_share/{photo}").status_code, 200)


class EventRuleTests(ShareTestCase):
    def test_hub_topics_reserved(self):
        AppInstall.objects.filter(app_id="sunpeaks").update(
            granted_permissions=["events.publish:_shares/#"], requested_permissions=["events.publish:_shares/#"])
        self.provider.refresh_from_db()
        with mock.patch("broker.events_bridge.publish") as pub:
            self.refused("events.publish",
                         {"topic": "_shares/sunpeaks/events", "data": {}}, "forbidden")
            self.refused("events.publish", {"topic": "_x"}, "forbidden")
        pub.assert_not_called()

    def test_subscribe_tolerates_hub_topics(self):
        with mock.patch("broker.events_bridge.set_patterns") as setp:
            self.ok("events.subscribe", {"patterns": ["_shares/sunpeaks/events", "sunpeaks/#"]})
        setp.assert_called_once_with("sunpeaks", ["sunpeaks/#"])
        self.assertEqual(events_bridge.allowed(self.provider, ["_shares/#", "sunpeaks/#"]), ["sunpeaks/#"])


class SharingScreenTests(ShareTestCase):
    def setUp(self):
        super().setUp()
        AppInstall.objects.update(runtime=AppInstall.CONTAINER)
        self.admin = User.objects.create_user("admin", password="pw", is_staff=True, is_superuser=True)
        self.url = "/settings/apps/sunpeaks/share-grant/"

    def test_candidates_filter_by_reads(self):
        from hub_settings import apps_views

        card = apps_views._sharing(AppInstall.objects.get(app_id="sunpeaks"))
        self.assertEqual([a["id"] for a in card[0]["readers"]], ["calendar"])
        self.assertEqual([a["id"] for a in card[0]["others"]], ["cams"])
        self.assertEqual(apps_views._sharing(AppInstall.objects.get(app_id="cams")), [])

    def test_grant_and_revoke(self):
        self.client.force_login(self.admin)
        resp = self.client.post(self.url, {"share": "events", "consumer": "calendar", "on": "on"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ShareGrant.objects.filter(consumer="calendar", granted_by="admin").exists())
        self.assertTrue(AuditEntry.objects.filter(app_id="sunpeaks", op="shares.grant").exists())
        from hub_settings import apps_views

        self.assertEqual(apps_views._receives(AppInstall.objects.get(app_id="calendar")),
                         ["Sun Peaks events (from Cameras)"])
        self.client.post(self.url, {"share": "events", "consumer": "calendar"})
        self.assertFalse(ShareGrant.objects.exists())
        self.assertTrue(AuditEntry.objects.filter(app_id="sunpeaks", op="shares.revoke").exists())
        self.assertEqual(self.client.post(self.url, {"share": "nope", "consumer": "calendar", "on": "on"})
                         .status_code, 404)
        self.assertEqual(self.client.post(self.url, {"share": "events", "consumer": "sunpeaks", "on": "on"})
                         .status_code, 404)

    def test_needs_admin(self):
        self.client.post(self.url, {"share": "events", "consumer": "calendar", "on": "on"})
        user = User.objects.create_user("guest", password="pw")
        self.client.force_login(user)
        self.client.post(self.url, {"share": "events", "consumer": "calendar", "on": "on"})
        self.assertFalse(ShareGrant.objects.exists())

    def test_needs_csrf(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        resp = client.post(self.url, {"share": "events", "consumer": "calendar", "on": "on"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ShareGrant.objects.exists())
