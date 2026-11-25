"""Clocks show the hub's time, not the browser's (the kiosk has stale time zone rules)."""
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hub.clock import utc_offset_minutes

EDMONTON = ZoneInfo("America/Edmonton")
ROOT = Path(settings.BASE_DIR)


class OffsetTests(SimpleTestCase):
    @patch("bdh_sdk.location.tzinfo", return_value=EDMONTON)
    def test_follows_dst_of_the_hub_zone(self, _tz):
        self.assertEqual(utc_offset_minutes(datetime(2026, 1, 15, tzinfo=timezone.utc)), -420)
        self.assertEqual(utc_offset_minutes(datetime(2026, 7, 15, tzinfo=timezone.utc)), -360)

    @patch("bdh_sdk.location.tzinfo", side_effect=RuntimeError("no settings"))
    def test_utc_when_the_zone_is_unknown(self, _tz):
        self.assertEqual(utc_offset_minutes(), 0)


class ClockEndpointTests(TestCase):
    @patch("hub.clock.utc_offset_minutes", return_value=-420)
    def test_endpoint_returns_offset(self, _off):
        resp = self.client.get(reverse("hub_clock"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"offset": -420})

    @patch("hub.clock.utc_offset_minutes", return_value=-420)
    def test_pages_carry_the_offset(self, _off):
        resp = self.client.get(reverse("login"))
        self.assertContains(resp, "var offset = -420;")
        self.assertContains(resp, "window.bdhClock")


class NoBrowserTimeZoneTests(SimpleTestCase):
    """The whole class of bug: any hub page formatting "now" in the browser's zone."""

    BROWSER_ZONE = re.compile(
        r"\.getHours\(|\.getDay\(|\.getDate\(|toLocaleTimeString|toLocaleDateString")
    DIRS = ("templates", "hub", "display", "hub_settings", "broker", "static")

    def test_hub_pages_use_bdh_clock(self):
        offenders = []
        for folder in self.DIRS:
            for path in (ROOT / folder).rglob("*"):
                if path.suffix not in (".html", ".js") or "node_modules" in path.parts:
                    continue
                for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if self.BROWSER_ZONE.search(line):
                        offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "Use window.bdhClock (templates/base.html) for local times")
