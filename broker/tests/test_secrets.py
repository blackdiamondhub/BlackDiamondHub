from unittest import mock

from broker.models import AuditEntry
from broker.ratelimit import limiter
from hub_settings import vault
from hub_settings.models import AppSecret

from .base import make_install
from .test_rpc import RpcTestCase

VALUE = "hunter2-sekrit-value"


class SecretsRpcTests(RpcTestCase):
    def setUp(self):
        super().setUp()
        vault.set_provider(vault.StaticKeyProvider(("k1", b"\x07" * 32)))
        self.addCleanup(vault.set_provider, None)
        self.other = make_install("other", upstream="http://127.0.0.1:9202")

    def test_round_trip(self):
        self.assertEqual(self.ok("secrets.get", {"name": "pw"}), {"found": False, "value": None})
        self.assertIsNone(self.ok("secrets.set", {"name": "pw", "value": VALUE}))
        self.assertEqual(self.ok("secrets.get", {"name": "pw"}), {"found": True, "value": VALUE})
        self.assertEqual(vault.get_secret("cams", "pw"), VALUE)
        self.ok("secrets.delete", {"name": "pw"})
        self.assertFalse(AppSecret.objects.exists())

    def test_ownership_comes_from_the_token(self):
        vault.set_secret("other", "pw", "theirs")
        # App "cams" naming another app gets (and writes) its own secret, never theirs.
        self.assertEqual(self.ok("secrets.get", {"name": "pw", "app": "other"}), {"found": False, "value": None})
        self.ok("secrets.set", {"name": "pw", "value": VALUE, "app": "other"})
        self.ok("secrets.delete", {"name": "pw", "app": "other"})
        self.assertEqual(vault.get_secret("other", "pw"), "theirs")
        self.assertEqual(self.ok("secrets.get", {"name": "pw"}, install=self.other)["value"], "theirs")

    def test_audit_has_the_name_never_the_value(self):
        self.ok("secrets.set", {"name": "pw", "value": VALUE})
        self.ok("secrets.get", {"name": "pw"})
        self.refused("secrets.set", {"name": "pw", "value": ""}, code="invalid", status=400)
        entries = AuditEntry.objects.filter(op__startswith="secrets.")
        self.assertEqual(entries.count(), 3)
        for entry in entries:
            self.assertEqual(entry.app_id, "cams")
            self.assertNotIn(VALUE, entry.detail)
            self.assertIn("pw", entry.detail)

    def test_bad_input(self):
        self.refused("secrets.get", {"name": "../x"}, code="invalid", status=400)
        self.refused("secrets.set", {"name": "pw", "value": 5}, code="invalid", status=400)
        self.refused("secrets.set", {"name": "pw", "value": "x" * 9000}, code="invalid", status=400)

    def test_no_key_is_a_clean_error(self):
        vault.set_secret("cams", "pw", VALUE)
        vault.set_provider(vault.StaticKeyProvider(("k2", b"\x08" * 32)))
        resp = self.refused("secrets.get", {"name": "pw"}, code="error", status=500)
        self.assertNotIn(VALUE, resp.content.decode())

    def test_rate_limited(self):
        self.assertEqual(sum(limiter.allow("cams", "secrets.set", now=100.0) for _ in range(10)), 5)
        with mock.patch("broker.ratelimit.time.monotonic", return_value=1000.0):
            statuses = [self.call("secrets.get", {"name": "pw"}).status_code for _ in range(25)]
        self.assertEqual(statuses[:20], [200] * 20)
        self.assertIn(429, statuses[20:])
