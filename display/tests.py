import importlib
import io
import os
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from bdh_sdk.provides import (LIVE, PHOTOS, SAVER, SEASON, STATS, WINTER, SaverImage, SaverSource, SeasonHint,
                              Stat)
from django.apps import apps as django_apps
from django.conf import settings
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

from hub.plugins import PluginSpec
from hub_settings import store
from tests.selenium_helpers import SeleniumTestCase, login_via_browser
from . import catalog, connect, views
from hub_settings.models import AppSetting
from .models import DisplayLoad, DisplaySample, Heartbeat, SaverPhoto, UploadLink
from .options import DEFAULT_STARTS, SEASONS, STOCK_EXTRAS, STOCK_STYLES
from .schedule import is_night, parse_hhmm
from .season import opening_day, parse_mmdd, resolve, winter_signal
from .stats import collect, days_until_lifts

STARTS = dict(DEFAULT_STARTS)  # winter 11-21, spring 03-15, summer 06-01, fall 09-15


class FakeState(dict):
    def set(self, key, value):
        self[key] = value


class SeasonTests(TestCase):
    def test_by_date(self):
        self.assertEqual(resolve(date(2026, 1, 5), STARTS, use_lifts=False), "winter")
        self.assertEqual(resolve(date(2026, 3, 15), STARTS, use_lifts=False), "spring")
        self.assertEqual(resolve(date(2026, 7, 1), STARTS, use_lifts=False), "summer")
        self.assertEqual(resolve(date(2026, 11, 20), STARTS, use_lifts=False), "fall")
        self.assertEqual(resolve(date(2026, 11, 21), STARTS, use_lifts=False), "winter")

    def test_lifts_bring_winter_forward_inside_window(self):
        state = FakeState()
        self.assertEqual(resolve(date(2026, 11, 1), STARTS, lifts_open=3, state=state), "winter")
        self.assertEqual(state["lifts_opened"], "2026-11-01")

    def test_lifts_ignored_outside_window(self):
        state = FakeState()
        self.assertEqual(resolve(date(2026, 10, 1), STARTS, lifts_open=3, state=state), "fall")
        self.assertNotIn("lifts_opened", state)

    def test_remembered_opening_sticks(self):
        state = FakeState(lifts_opened="2026-11-01")
        self.assertEqual(resolve(date(2026, 11, 5), STARTS, lifts_open=None, state=state), "winter")

    def test_last_years_opening_is_ignored(self):
        state = FakeState(lifts_opened="2025-11-01")
        self.assertEqual(resolve(date(2026, 11, 5), STARTS, lifts_open=0, state=state), "fall")

    def test_lifts_off(self):
        self.assertEqual(resolve(date(2026, 11, 1), STARTS, lifts_open=3, use_lifts=False), "fall")

    def test_scheduled_hint_counts_from_its_day(self):
        hints = [{"season": "winter", "opens_on": "2026-11-26"}]
        self.assertFalse(winter_signal(hints, date(2026, 11, 25), STARTS))
        self.assertTrue(winter_signal(hints, date(2026, 11, 26), STARTS))
        self.assertTrue(winter_signal([{"season": "winter"}], date(2026, 11, 25), STARTS))
        self.assertFalse(winter_signal([{"season": "summer"}], date(2026, 11, 25), STARTS))

    def test_stale_or_implausible_opening_day_says_nothing(self):
        # Last year's date still on the resort's page must not bring winter forward.
        self.assertFalse(winter_signal([{"season": "winter", "opens_on": "2025-11-26"}], date(2026, 11, 1), STARTS))
        self.assertFalse(winter_signal([{"season": "winter", "opens_on": "2026-07-01"}], date(2026, 7, 2), STARTS))
        self.assertFalse(winter_signal([{"season": "winter", "opens_on": "garbage"}], date(2026, 11, 1), STARTS))

    def test_opening_day(self):
        today = date(2026, 11, 24)
        hints = [{"season": "winter", "opens_on": "2026-12-05"}, {"season": "winter", "opens_on": "2026-11-26"},
                 {"season": "winter"}, {"season": "winter", "opens_on": "2026-11-24"}]
        self.assertEqual(opening_day(hints, today, STARTS), date(2026, 11, 26))
        self.assertIsNone(opening_day([{"season": "winter", "opens_on": "2027-03-01"}], today, STARTS))
        self.assertIsNone(opening_day([{"season": "summer", "opens_on": "2026-11-26"}], today, STARTS))
        self.assertIsNone(opening_day([], today, STARTS))

    def test_parse_mmdd(self):
        self.assertEqual(parse_mmdd("02-29"), (2, 29))
        for bad in ("11/21", "1-21", "13-01", "02-30", "", "abc"):
            with self.assertRaisesMessage(ValueError, "Use MM-DD"):
                parse_mmdd(bad)


class StatTests(TestCase):
    def test_days_until_lifts(self):
        today = date(2026, 11, 11)
        self.assertEqual(days_until_lifts(today, STARTS, False)["value"], "10 days")
        self.assertIsNone(days_until_lifts(today, STARTS, True))
        self.assertIsNone(days_until_lifts(today, STARTS, None))

    def test_days_until_announced_opening(self):
        # Opening day moved past the winter start setting: keep counting down to it.
        today = date(2026, 11, 24)
        self.assertIsNone(days_until_lifts(today, STARTS, False))
        self.assertEqual(days_until_lifts(today, STARTS, False, date(2026, 11, 26))["value"], "2 days")
        self.assertEqual(days_until_lifts(today, STARTS, False, date(2026, 11, 25))["value"], "1 day")
        self.assertIsNone(days_until_lifts(today, STARTS, True, date(2026, 11, 26)))

    @mock.patch("hub.provides.stats", return_value={})
    @mock.patch("hub.provides.stat_owners", return_value=set())
    def test_missing_data_is_skipped(self, _owners, stats):
        # Isolated from the installed apps' providers, which would fetch live data.
        keys = ["snow_report.temp_top", "none", "snow_report.base", "snow_report.wind_top", "lift_status.groomed"]
        self.assertEqual(collect(keys, date(2026, 11, 11), STARTS), [])
        stats.assert_not_called()


P = "display.tests."
PROVIDED = {}


def weather_stats():
    return [Stat("temp", "Outside", value="12°C"), Stat("feels_like", "Feels like", value="10°C"),
            Stat("sun", "Sunset", value="4:30 PM")]


def snow_stats():
    return [Stat("temp_top", "Up top", value="-6°C"), Stat("temp_village", "At the lodge", value="0°C"),
            Stat("snow_48h", "48h snow", value=PROVIDED.get("snow", "20 cm"))]


def lift_stats():
    return [Stat("runs_open", "Runs open", value="112 of 138")]


CALLED = []


def webcams():
    CALLED.append("webcams")
    return [SaverImage("cams", "peak", "/cam/peak.jpg", label="Peak cam", refresh_seconds=30),
            SaverImage("cams", "village", "/cam/village.jpg", label="Village cam", refresh_seconds=60)]


def gallery():
    CALLED.append("gallery")
    return [SaverImage("shots", f"s{n}", f"/shots/{n}.jpg", label=f"Shot {n}", caption=f"Day {n}") for n in range(3)]


def one_cam():
    return [SaverImage("cam", "deck", "/static/android-chrome-512x512.png", label="Deck cam", refresh_seconds=60)]


def two_cams():
    return [SaverImage("cams", "a", "/static/android-chrome-512x512.png", label="Cam A", refresh_seconds=60),
            SaverImage("cams", "b", "/static/android-chrome-192x192.png", label="Cam B", refresh_seconds=60)]


def lifts_hint():
    return SeasonHint(WINTER, "Lifts are open") if PROVIDED.get("open") else None


def opening_hint():
    return SeasonHint(WINTER, "Opening day", opens_on=date(2026, 11, 26))


_OWN = ("snow_report", "lift_status", "sunpeaks_webcams", "weather")
# Installed apps that hint at the season would change what these tests expect.
BASE = tuple(s for s in settings.HUB_PLUGINS if s.id not in _OWN and SEASON not in s.provides)
SNOW = PluginSpec(id="snow_report", app="snow_report", provides={STATS: P + "snow_stats"},
                  stat_keys=(("temp_top", "Up top"), ("temp_village", "At the lodge"), ("snow_48h", "48h snow")))
LIFTS = PluginSpec(id="lift_status", app="lift_status", provides={STATS: P + "lift_stats"},
                   stat_keys=(("runs_open", "Runs open"),))
CAMS = PluginSpec(id="sunpeaks_webcams", app="sunpeaks_webcams", provides={SAVER: P + "webcams"},
                  saver_sources=(SaverSource("cams", "Mountain cams", "Live views from the hill", LIVE),))
GALLERY = PluginSpec(id="gallery", app="gallery", provides={SAVER: P + "gallery"},
                     saver_sources=(SaverSource("shots", "Lodge shots", "Photos of the lodge", PHOTOS),))
HILL = PluginSpec(id="hillcams", app="hillcams", provides={SAVER: P + "two_cams"},
                  saver_sources=(SaverSource("cams", "Hill cams", "Two cameras up the hill", LIVE),))
DECK = PluginSpec(id="deckcam", app="deckcam", provides={SAVER: P + "one_cam"},
                  saver_sources=(SaverSource("cam", "Deck cam", "The view from the deck", LIVE),))
MOUNTAIN = BASE + (SNOW, LIFTS, CAMS)
MOUNTAIN_SLOTS = ["snow_report.temp_top", "snow_report.temp_village", "snow_report.snow_48h", "lift_status.runs_open"]
WEATHER = PluginSpec(id="weather", app="weather", provides={STATS: P + "weather_stats"},
                     stat_keys=(("temp", "Outside"), ("feels_like", "Feels like"), ("sun", "Next sunrise or sunset")))
# No site slot defaults: the display's generic ones apply.
NO_SITE_SLOTS = {f"BDH_DISPLAY_SLOTS_{s}": None for s in ("WINTER", "SPRING", "SUMMER", "FALL")}

@override_settings(HUB_PLUGINS=MOUNTAIN, BDH_DISPLAY_SLOTS_WINTER=MOUNTAIN_SLOTS)
class ViewTests(TestCase):
    def setUp(self):
        store.clear()
        cache.clear()
        PROVIDED.clear()
        self.addCleanup(store.clear)
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.guest = User.objects.create_user("guest", password="pw")

    def test_saver_api(self):
        body = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
        self.assertEqual(body["season"], "winter")
        self.assertEqual(body["sources"], ["stock"])
        self.assertEqual([s["label"] for s in body["stats"]], ["Up top", "At the lodge", "48h snow", "Runs open"])

    def test_saver_api_stock_art(self):
        body = self.client.get(reverse("display_saver_api"), {"season": "fall"}).json()
        self.assertEqual(len(body["stock"]), 8)
        self.assertTrue(all(u.startswith(settings.STATIC_URL + "display/stock/fall/") for u in body["stock"]))
        for season in ("winter", "spring", "summer", "fall"):
            for url in views.stock_art(season):
                self.assertTrue(finders.find(url[len(settings.STATIC_URL):]), url)
        winter = views.stock_art("winter")
        self.assertEqual(len(winter), 12)
        self.assertTrue(winter[-1].endswith("/display/stock/winter/family_fireside.webp"))
        summer = views.stock_art("summer")
        self.assertEqual(len(summer), 9)
        self.assertTrue(summer[-1].endswith("/display/stock/summer/lake_kids.webp"))

    def api(self, season="summer", **params):
        store.clear()
        return self.client.get(reverse("display_saver_api"), {"season": season, **params}).json()

    def test_saver_api_live_source_rotates_or_shows_the_pick(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="sunpeaks_webcams.cams")
        body = self.api()
        self.assertEqual(body["sources"], ["sunpeaks_webcams.cams"])
        self.assertEqual(body["layers"], [{
            "id": "sunpeaks_webcams.cams", "kind": "live", "title": "Mountain cams", "pick": None, "items": [
                {"id": "peak", "url": "/cam/peak.jpg", "label": "Peak cam", "refresh_seconds": 30},
                {"id": "village", "url": "/cam/village.jpg", "label": "Village cam", "refresh_seconds": 60}]}])
        AppSetting.objects.create(app_id="display", key="saver_pick", value="sunpeaks_webcams.cams/village")
        layer = self.api()["layers"][0]
        self.assertEqual((layer["pick"], [i["id"] for i in layer["items"]]), ("village", ["village"]))
        AppSetting.objects.filter(key="saver_pick").update(value="sunpeaks_webcams.cams/gone")
        layer = self.api()["layers"][0]
        self.assertEqual((layer["pick"], len(layer["items"])), (None, 2))  # a camera that's gone: rotate

    def test_saver_api_photos_source_from_an_app(self):
        with override_settings(HUB_PLUGINS=MOUNTAIN + (GALLERY,)):
            AppSetting.objects.create(app_id="display", key="source_summer", value="gallery.shots")
            body = self.api()
            self.assertEqual(body["sources"], ["gallery.shots"])
            layer = body["layers"][0]
            self.assertEqual((layer["kind"], layer["title"]), ("photos", "Lodge shots"))
            self.assertEqual(layer["items"][0], {"id": "s0", "url": "/shots/0.jpg", "label": "Shot 0", "caption": ""})
            AppSetting.objects.create(app_id="display", key="photo_caption", value=True)
            self.assertEqual([i["caption"] for i in self.api()["layers"][0]["items"]], ["Day 0", "Day 1", "Day 2"])

    def test_saver_api_calls_only_the_apps_it_needs(self):
        with override_settings(HUB_PLUGINS=MOUNTAIN + (GALLERY,)):
            CALLED.clear()
            self.assertEqual(self.api()["layers"], [])  # stock: no app is asked
            self.assertEqual(CALLED, [])
            AppSetting.objects.create(app_id="display", key="source_summer", value="gallery.shots")
            self.api()
            self.assertEqual(CALLED, ["gallery"])

    def test_saver_api_mix_takes_turns_within_a_cap(self):
        with override_settings(HUB_PLUGINS=MOUNTAIN + (GALLERY,)):
            AppSetting.objects.create(app_id="display", key="source_summer", value="mix")
            body = self.api()
            self.assertEqual(body["sources"], ["stock", "sunpeaks_webcams.cams", "gallery.shots"])  # no photos yet
            self.assertEqual([len(layer["items"]) for layer in body["layers"]], [2, 3])
            with mock.patch("display.views.MIX_IMAGES", 3):
                body = self.api()
            self.assertEqual([[i["id"] for i in layer["items"]] for layer in body["layers"]],
                             [["peak", "village"], ["s0"]])

    def test_old_saved_sources_still_work(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="cam")
        AppSetting.objects.create(app_id="display", key="source_fall", value="sunpeaks_webcams.backgrounds")
        AppSetting.objects.create(app_id="display", key="cam", value="sunpeaks_webcams.village")
        for season in ("summer", "fall"):
            body = self.api(season)
            self.assertEqual(body["sources"], ["sunpeaks_webcams.cams"], season)
            self.assertEqual([i["id"] for i in body["layers"][0]["items"]], ["village"])
        self.assertEqual(catalog.resolve_pick(), "sunpeaks_webcams.cams/village")
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"), {"season": "summer"})
        self.assertRegex(page.content.decode(),
                         r'value="sunpeaks_webcams.cams" data-kind="live"\s+data-count="2" data-available checked')
        self.assertContains(page, '<option value="sunpeaks_webcams.cams/village" selected>Village cam</option>')
        with override_settings(HUB_PLUGINS=BASE + (GALLERY,)):
            self.assertEqual(self.api()["sources"], ["stock"])  # no live source left
            self.assertEqual(catalog.resolve_pick(), "")

    def test_saving_rotate_overrides_an_old_cam_pick(self):
        # Sun Peaks bug: choosing "Rotate through all" with an old ``cam`` saved snapped
        # back to that camera, because "" is the default and wasn't stored.
        AppSetting.objects.create(app_id="display", key="source_summer", value="sunpeaks_webcams.cams")
        AppSetting.objects.create(app_id="display", key="cam", value="sunpeaks_webcams.village")
        self.client.force_login(self.staff)
        url = reverse("display_settings")
        for pick in ("", "sunpeaks_webcams.cams/peak", ""):
            self.assertEqual(self.client.post(url, self.form(source_summer="sunpeaks_webcams.cams",
                                                             saver_pick=pick)).status_code, 302, pick)
            self.assertEqual(catalog.resolve_pick(), pick)
            layer = self.api()["layers"][0]
            self.assertEqual((layer["pick"], len(layer["items"])),
                             (None, 2) if not pick else ("peak", 1), pick)
            page = self.client.get(url, {"season": "summer"}).content.decode()
            self.assertNotIn('value="sunpeaks_webcams.cams/village" selected', page)
        self.assertFalse(AppSetting.objects.filter(app_id="display", key="cam").exists())

    def test_unavailable_source_is_kept_and_falls_back(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="ticker.cams")
        body = self.api()
        self.assertEqual((body["sources"], body["layers"]), (["stock"], []))
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"), {"season": "summer"})
        self.assertContains(page, "ticker.cams (unavailable)")
        self.assertEqual(self.client.post(reverse("display_settings"), self.form()).status_code, 302)
        self.assertEqual(store.saved("display")["source_summer"], "ticker.cams")

    def test_saver_choices_in_order(self):
        with override_settings(HUB_PLUGINS=MOUNTAIN + (GALLERY,)):
            choices = catalog.saver_choices(["ticker.cams", "stock"])
        self.assertEqual([(c["id"], c["kind"], c["available"]) for c in choices], [
            ("stock", "stock", True), ("photos", "photos", True), ("sunpeaks_webcams.cams", "live", True),
            ("gallery.shots", "photos", True), ("ticker.cams", "", False), ("mix", "mix", True)])
        self.assertEqual(choices[2]["description"], "Live views from the hill")
        self.assertEqual(choices[-1]["description"], "Paintings, photos, Mountain cams & Lodge shots")
        with override_settings(HUB_PLUGINS=BASE):
            self.assertEqual(catalog.saver_choices()[-1]["description"], "Paintings & photos")

    def test_settings_shows_each_app_source(self):
        with override_settings(HUB_PLUGINS=MOUNTAIN + (GALLERY,)):
            self.client.force_login(self.staff)
            page = self.client.get(reverse("display_settings"), {"season": "summer"})
        html = page.content.decode()
        order = [html.index(f'name="source_summer" value="{i}"')
                 for i in ("stock", "photos", "sunpeaks_webcams.cams", "gallery.shots", "mix")]
        self.assertEqual(order, sorted(order))
        self.assertContains(page, "Mountain cams</b>")
        self.assertContains(page, "<small>Live views from the hill</small>")
        # The app badge on both app cards, in each season's list of sources.
        self.assertContains(page, 'class="dsp-app" title="From the', count=2 * len(SEASONS))
        # Only the live source with more than one camera gets a picker; only the chosen source's controls show.
        self.assertContains(page, 'data-pick-for="sunpeaks_webcams.cams"', count=1)
        self.assertNotContains(page, 'data-pick-for="gallery.shots"')
        self.assertContains(page, '<div class="dsp-for" data-for-src="stock">')
        self.assertContains(page, '<div class="dsp-for" data-for-src="sunpeaks_webcams.cams" hidden>')
        self.assertContains(page, '<label class="dsp-line" id="dsp-rot-row">')
        self.assertContains(page, 'id="dsp-caption-row" hidden>')

    def test_settings_controls_for_a_picked_camera(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="sunpeaks_webcams.cams")
        AppSetting.objects.create(app_id="display", key="saver_pick", value="sunpeaks_webcams.cams/peak")
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"), {"season": "summer"})
        self.assertContains(page, '<div class="dsp-for" data-for-src="sunpeaks_webcams.cams">')
        self.assertContains(page, 'id="dsp-rot-row" hidden>')  # one camera: nothing rotates
        self.assertContains(page, "/cam/peak.jpg")  # the preview's pictures

    def test_settings_rejects_unknown_sources_and_picks(self):
        url = reverse("display_settings")
        self.assertEqual(self.client.post(url, self.form(source_summer="ticker.cams")).status_code, 400)
        self.assertEqual(self.client.post(url, self.form(source_summer="<b>")).status_code, 400)
        for bad in ("gallery.shots/s1", "sunpeaks_webcams.cams", "ticker.cams/x"):
            response = self.client.post(url, self.form(saver_pick=bad))
            self.assertContains(response, "Pick a picture from the list", status_code=400)
        self.client.post(url, self.form(source_summer="sunpeaks_webcams.cams", saver_pick="sunpeaks_webcams.cams/peak"))
        saved = store.saved("display")
        self.assertEqual((saved["source_summer"], saved["saver_pick"]),
                         ("sunpeaks_webcams.cams", "sunpeaks_webcams.cams/peak"))

    def test_preview_source_and_pick_are_staff_only(self):
        preview = {"source": "sunpeaks_webcams.cams", "pick": "sunpeaks_webcams.cams/peak"}
        self.assertEqual(self.api(**preview)["sources"], ["stock"])
        self.client.force_login(self.staff)
        body = self.api(**preview)
        self.assertEqual([i["id"] for i in body["layers"][0]["items"]], ["peak"])
        self.assertEqual(self.api(source="nope.cams")["sources"], ["stock"])

    def test_container_cam_url_may_wake_its_app(self):
        self.assertEqual(views._waking("/apps/cams/_raw/latest/peak.jpg"),
                         "/apps/cams/_raw/latest/peak.jpg?_bdh_wake=1")
        self.assertEqual(views._waking("/apps/cams/_raw/x.jpg?a=1"), "/apps/cams/_raw/x.jpg?a=1&_bdh_wake=1")
        self.assertEqual(views._waking("http://cam/peak.jpg"), "http://cam/peak.jpg")

    def test_site_slot_defaults(self):
        body = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
        self.assertEqual(len(body["stats"]), 4)
        with override_settings(BDH_DISPLAY_SLOTS_WINTER="lift_status.runs_open, snow_report.temp_top"):
            body = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
        self.assertEqual([s["label"] for s in body["stats"]], ["Runs open", "Up top"])
        AppSetting.objects.create(app_id="display", key="slot_winter_1", value="snow_report.snow_48h")
        body = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
        self.assertEqual(body["stats"][0]["label"], "48h snow")
    def test_unavailable_slot_is_kept_and_skipped(self):
        AppSetting.objects.create(app_id="display", key="slot_winter_2", value="ticker.price")
        body = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
        self.assertEqual([s["label"] for s in body["stats"]], ["Up top", "48h snow", "Runs open"])
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"))
        self.assertContains(page, '<option value="ticker.price" selected>ticker.price (unavailable)</option>')
        self.client.post(reverse("display_settings"), self.form())
        self.assertEqual(store.saved("display")["slot_winter_2"], "ticker.price")

    def test_season_hint_brings_winter_forward(self):
        hint = PluginSpec(id="lift_status", app="lift_status", provides={SEASON: P + "lifts_hint"})
        with override_settings(HUB_PLUGINS=BASE + (hint,)), \
                mock.patch("display.views.lodge_today", return_value=date(2026, 11, 5)):
            self.assertEqual(self.client.get(reverse("display_saver_api")).json()["season"], "fall")
            cache.clear()
            PROVIDED["open"] = True
            self.assertEqual(self.client.get(reverse("display_saver_api")).json()["season"], "winter")

    def test_announced_opening_day_drives_countdown(self):
        opening = PluginSpec(id="resort_events", app="resort_events", provides={SEASON: P + "opening_hint"})
        lifts = PluginSpec(id="lift_status", app="lift_status", provides={SEASON: P + "lifts_hint"})

        def saver(day):
            cache.clear()
            with mock.patch("display.views.lodge_today", return_value=day):
                return self.client.get(reverse("display_saver_api")).json()

        with override_settings(HUB_PLUGINS=BASE + (opening, lifts), BDH_DISPLAY_SLOTS_WINTER="days_until_lifts",
                               BDH_DISPLAY_SLOTS_FALL="days_until_lifts"):
            body = saver(date(2026, 11, 19))
            self.assertEqual((body["season"], body["stats"]), ("fall", [{"label": "Until lifts open", "value": "7 days"}]))
            body = saver(date(2026, 11, 24))  # past the 11-21 setting, still closed
            self.assertEqual((body["season"], body["stats"]), ("winter", [{"label": "Until lifts open", "value": "2 days"}]))
            self.assertEqual(saver(date(2026, 11, 26))["stats"], [])
            PROVIDED["open"] = True
            self.assertEqual(saver(date(2026, 11, 24))["stats"], [])

    def test_site_without_mountain_apps(self):
        with override_settings(HUB_PLUGINS=BASE + (WEATHER,), **NO_SITE_SLOTS):
            body = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
            self.assertEqual(body["stats"], [{"label": "Outside", "value": "12°C"},
                                             {"label": "Feels like", "value": "10°C"},
                                             {"label": "Sunset", "value": "4:30 PM"}])
            self.client.force_login(self.staff)
            page = self.client.get(reverse("display_settings"))
        self.assertContains(page, '<optgroup label="Weather">')
        self.assertNotContains(page, 'name="cam"')
        self.assertNotContains(page, 'data-pick-for="')
        self.assertNotContains(page, 'id="dsp-lifts"')
        self.assertContains(page, '<input type="hidden" name="winter_on_lifts" value="on">')
        self.assertContains(page, "<small>Paintings &amp; photos</small>")  # Mix, with no app sources
        self.assertNotContains(page, "Lift")
        self.assertNotContains(page, "lifts open")

    def test_settings_staff_only(self):
        url = reverse("display_settings")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.guest)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_settings_page_and_nav(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("display_settings"))
        self.assertContains(response, "Screensaver, home screen &amp; schedule")
        self.assertContains(response, f'href="{reverse("display_settings")}"')
        self.assertContains(response, 'name="slot_fall_4"')

    def test_generic_app_page_redirects(self):
        self.client.force_login(self.staff)
        response = self.client.get("/settings/app/display/")
        self.assertRedirects(response, reverse("display_settings"), fetch_redirect_response=False)

    def form(self, **overrides):
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"))
        data = {}
        for s in page.context["seasons"]:
            data[s["start_key"]] = s["start"]
            data[f"source_{s['id']}"] = s["source"]
            data.update({slot["key"]: slot["value"] for slot in s["slots"]})
        data.update(idle_minutes="10", burn_in="on", winter_on_lifts="on", rotate_seconds="45", saver_pick="", tab="fall",
                    night="on", night_from="23:00", night_until="06:00", chime_volume="70",
                    home_bg="mountains", home_tiles="solid", home_dim="45")
        data.update(overrides)
        return data

    def test_save(self):
        response = self.client.post(reverse("display_settings"), self.form(winter_start="11-1", idle_minutes="15"))
        self.assertEqual(response.status_code, 400)  # 11-1 isn't MM-DD
        self.assertContains(response, "Use MM-DD", status_code=400)
        self.assertContains(response, " data-dirty>", status_code=400)
        page = self.client.get(reverse("display_settings"))
        self.assertContains(page, 'class="hst-btn" data-save>Save')
        self.assertRegex(page.content.decode(), r'id="dsp-tab" value="\w+" data-clean')  # switching tabs isn't a change
        self.assertNotContains(page, " data-dirty>")

        response = self.client.post(reverse("display_settings"), self.form(winter_start="11-01", idle_minutes="15"))
        self.assertRedirects(response, reverse("display_settings") + "?saved=1&season=fall",
                             fetch_redirect_response=False)
        saved = store.saved("display")
        self.assertEqual(saved["winter_start"], "11-01")
        self.assertEqual(saved["idle_minutes"], 15)
        self.assertNotIn("burn_in", saved)  # unchanged defaults stay unsaved
        self.assertNotIn("slot_winter_1", saved)  # so slots follow the site's apps

        self.client.post(reverse("display_settings"), self.form(slot_winter_1="lift_status.groomed"))
        self.assertEqual(store.saved("display")["slot_winter_1"], "lift_status.groomed")
        response = self.client.post(reverse("display_settings"), self.form(slot_winter_1="<b>"))
        self.assertEqual(response.status_code, 400)

    def test_night_times(self):
        response = self.client.post(reverse("display_settings"), self.form(night_from="25:00"))
        self.assertContains(response, "Use HH:MM", status_code=400)

        self.client.post(reverse("display_settings"), self.form(night_from="22:30", night_until="7:00"))
        saved = store.saved("display")
        self.assertEqual((saved["night_from"], saved["night_until"]), ("22:30", "07:00"))

        with mock.patch("display.views.lodge_minute", return_value=23 * 60):
            self.assertTrue(self.client.get(reverse("display_schedule_api")).json()["dark"])
        with mock.patch("display.views.lodge_minute", return_value=12 * 60):
            body = self.client.get(reverse("display_schedule_api")).json()
        self.assertEqual(body, {"enabled": True, "from": "22:30", "until": "07:00", "dark": False})

        self.client.post(reverse("display_settings"), {k: v for k, v in self.form().items() if k != "night"})
        body = self.client.get(reverse("display_schedule_api")).json()
        self.assertEqual(body, {"enabled": False, "from": None, "until": None, "dark": False})

    def test_chime_volume(self):
        self.assertEqual(self.client.post(reverse("display_settings"), self.form(chime_volume="120")).status_code, 400)
        self.client.post(reverse("display_settings"), self.form(chime_volume="35"))
        self.assertEqual(store.saved("display")["chime_volume"], 35)
        with mock.patch.object(store, "TTL", 0):
            page = self.client.get(reverse("display_settings"))
        self.assertContains(page, 'name="chime_volume" min="0" max="100" step="5" value="35"')
        self.assertContains(page, '"chime_volume": 35')  # the saver config chimes.js reads
        self.assertContains(page, "hub/chimes.js")

    def home(self):
        # Hook threads (this test's or ones left over from earlier tests) use their own
        # DB connection, can't see this test's uncommitted settings and cache them
        # empty; a zero TTL makes the page read through this test's connection.
        empty = {"status": {}, "shortcuts": [], "refreshing": False}
        with mock.patch("BlackDiamondHub.views.home_data", return_value=empty), \
                mock.patch("BlackDiamondHub.views.lodge_card", return_value=None), \
                mock.patch.object(store, "TTL", 0):
            return self.client.get(reverse("landing_page"))

    def test_home_theme_settings(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"))
        self.assertContains(page, '<input type="radio" name="home_bg" value="mountains" checked>')
        self.assertContains(page, '<input type="radio" name="home_tiles" value="solid" checked>')
        self.assertContains(page, 'name="home_dim" min="0" max="80" step="5" value="45"')

        for bad in ({"home_tiles": "neon"}, {"home_bg": "video"}, {"home_dim": "90"}):
            self.assertEqual(self.client.post(reverse("display_settings"), self.form(**bad)).status_code, 400, bad)
        self.client.post(reverse("display_settings"), self.form(home_bg="art", home_tiles="frosted", home_dim="60"))
        saved = store.saved("display")
        self.assertEqual((saved["home_bg"], saved["home_tiles"], saved["home_dim"]), ("art", "frosted", 60))
        page = self.client.get(reverse("display_settings"))
        self.assertContains(page, '<input type="radio" name="home_tiles" value="frosted" checked>')
        self.assertNotContains(page, 'name="units"')

    @override_settings(BDH_HUB_UNITS="metric")
    def test_home_uses_hub_units(self):
        self.assertContains(self.home(), 'data-unit="C"')
        AppSetting.objects.create(app_id="hub", key="units", value="imperial")
        with mock.patch.object(store, "TTL", 0):
            self.assertContains(self.home(), 'data-unit="F"')

    def test_home_defaults_unchanged(self):
        page = self.home()
        self.assertContains(page, 'class="hub-home mode-')
        self.assertContains(page, " tiles-solid\">")
        self.assertNotContains(page, 'id="hub-bg"')
        self.assertContains(page, 'id="hub-sky"')

    def test_home_seasonal_art_and_glass(self):
        AppSetting.objects.create(app_id="display", key="home_bg", value="art")
        AppSetting.objects.create(app_id="display", key="home_tiles", value="dark")
        AppSetting.objects.create(app_id="display", key="home_dim", value=30)
        today = date(2026, 1, 10)
        with mock.patch("display.views.lodge_today", return_value=today), \
                mock.patch("display.views.current_season_safe", return_value="winter"):
            page = self.home()
        art = views.stock_art("winter")
        self.assertContains(page, " tiles-dark has-bg")
        self.assertContains(page, f"--dim: 0.30; background-image: url('{art[today.toordinal() % len(art)]}')")

    def test_home_crystal_glass(self):
        AppSetting.objects.create(app_id="display", key="home_bg", value="art")
        AppSetting.objects.create(app_id="display", key="home_tiles", value="crystal")
        with mock.patch("display.views.current_season_safe", return_value="winter"):
            page = self.home()
        self.assertContains(page, " tiles-crystal has-bg")

    def test_home_photos_fall_back_to_art(self):
        AppSetting.objects.create(app_id="display", key="home_bg", value="photos")
        with mock.patch("display.views.current_season_safe", return_value="summer"), \
                mock.patch.object(store, "TTL", 0):
            theme = views.home_theme(date(2026, 7, 1))
            self.assertIn(theme["bg"], views.stock_art("summer"))
            with mock.patch("display.views.season_photos", return_value=[{"url": "/media/a.jpg", "caption": ""}]):
                self.assertEqual(views.home_theme(date(2026, 7, 1))["bg"], "/media/a.jpg")

    def test_settings_previews_each_home_background(self):
        AppSetting.objects.create(app_id="display", key="home_tiles", value="frosted")
        self.client.force_login(self.staff)
        with mock.patch("display.views.current_season_safe", return_value="summer"), \
                mock.patch("display.views.season_photos", return_value=[{"url": "/media/a.jpg", "caption": ""}]), \
                mock.patch.object(store, "TTL", 0):
            page = self.client.get(reverse("display_settings"))
        self.assertContains(page, '<div class="dsp-mini tiles-frosted" id="dsp-mini"')
        pics = page.context["home_pictures"]
        self.assertEqual(pics["mountains"], None)
        self.assertIn(pics["art"], views.stock_art("summer"))
        self.assertEqual(pics["photos"], "/media/a.jpg")
        self.assertContains(page, 'id="dsp-home-pics"')

    def test_home_preview_never_breaks_settings(self):
        self.client.force_login(self.staff)
        with mock.patch("display.views.home_picture", side_effect=RuntimeError), \
                mock.patch.object(store, "TTL", 0):
            page = self.client.get(reverse("display_settings"))
        self.assertEqual(page.context["home_pictures"], {})

    def test_home_theme_never_breaks_home(self):
        AppSetting.objects.create(app_id="display", key="home_bg", value="art")
        with mock.patch("display.views.stock_art", side_effect=RuntimeError), \
                mock.patch.object(store, "TTL", 0):
            self.assertEqual(views.home_theme(), views.HOME_DEFAULT)
            self.assertEqual(self.home().status_code, 200)


class RotateMigrationTests(TestCase):
    """0003 turns the old "Change every" minutes into seconds within 30 s - 10 min."""

    def test_to_seconds(self):
        mig = importlib.import_module("display.migrations.0003_rotate_seconds")
        self.assertEqual([mig.to_seconds(v) for v in (3, "2", 60, 0, "x", None)], [180, 120, 600, 30, None, None])

    def test_forwards_moves_saved_value(self):
        mig = importlib.import_module("display.migrations.0003_rotate_seconds")
        AppSetting.objects.create(app_id="display", key="rotate_minutes", value=5)
        AppSetting.objects.create(app_id="other", key="rotate_minutes", value=5)
        mig.forwards(django_apps, None)
        self.assertEqual(AppSetting.objects.get(app_id="display", key="rotate_seconds").value, 300)
        self.assertFalse(AppSetting.objects.filter(app_id="display", key="rotate_minutes").exists())
        self.assertTrue(AppSetting.objects.filter(app_id="other", key="rotate_minutes").exists())
        mig.backwards(django_apps, None)
        self.assertEqual(AppSetting.objects.get(app_id="display", key="rotate_minutes").value, 5)
        self.assertFalse(AppSetting.objects.filter(key="rotate_seconds").exists())

    def test_range_clamps_saved_value(self):
        mig = importlib.import_module("display.migrations.0004_rotate_range")
        AppSetting.objects.create(app_id="display", key="rotate_seconds", value=600)
        mig.forwards(django_apps, None)
        self.assertEqual(AppSetting.objects.get(app_id="display", key="rotate_seconds").value, 90)
        self.assertEqual([mig.clamp(v) for v in (600, 45, 5, "x", None)], [90, 45, 15, None, None])


class HeartbeatTests(TestCase):
    def beat(self, ip, path="/"):
        return Client(enforce_csrf_checks=True).post(reverse("display_heartbeat"), {"path": path},
                                                     REMOTE_ADDR=ip, HTTP_USER_AGENT="Screen")

    def clients(self):
        return self.client.get(reverse("display_heartbeats")).json()["clients"]

    def test_records_latest_per_ip(self):
        self.assertEqual(self.beat("192.168.1.99", "/").status_code, 204)
        self.beat("192.168.1.99", "/cameras/")
        self.beat("192.168.1.32")
        rows = self.clients()
        self.assertEqual([(r["ip"], r["path"]) for r in rows], [("192.168.1.32", "/"), ("192.168.1.99", "/cameras/")])
        self.assertEqual(rows[1]["user_agent"], "Screen")
        self.assertLess(rows[1]["age_seconds"], 5)

    def test_reports_age_and_prunes_old(self):
        Heartbeat.objects.create(ip="192.168.1.99", last_seen=timezone.now() - timedelta(minutes=10))
        Heartbeat.objects.create(ip="192.168.1.50", last_seen=timezone.now() - timedelta(days=8))
        self.assertEqual([(r["ip"], r["age_seconds"] // 60) for r in self.clients()],
                         [("192.168.1.50", 8 * 24 * 60), ("192.168.1.99", 10)])
        self.beat("192.168.1.32")
        self.assertEqual([r["ip"] for r in self.clients()], ["192.168.1.32", "192.168.1.99"])

    def test_every_page_sends_heartbeats(self):
        html = self.client.get("/").content.decode()
        self.assertIn(reverse("display_heartbeat"), html)


class PageLoadTests(TestCase):
    def send(self, ip="192.168.1.99", **data):
        return Client(enforce_csrf_checks=True).post(reverse("display_page_load"), data, REMOTE_ADDR=ip)

    def loads(self, **params):
        return self.client.get(reverse("display_page_loads"), params).json()["loads"]

    def test_flags_unclean_previous_page_as_crash(self):
        self.assertEqual(self.send(path="/apps/cameras/", nav_type="navigate", heap_mb="40").status_code, 204)
        self.send(path="/", nav_type="navigate", prev_path="/apps/cameras/", prev_clean="0",
                  prev_lived_s="300", prev_silent_s="3", prev_heap_mb="512")
        self.send(ip="192.168.1.50", path="/", prev_path="/", prev_clean="1")
        newest, first = self.loads(ip="192.168.1.99")
        self.assertEqual((newest["path"], newest["prev_path"], newest["crash"]), ("/", "/apps/cameras/", True))
        self.assertEqual((newest["prev_lived_s"], newest["prev_silent_s"], newest["prev_heap_mb"]), (300, 3, 512))
        self.assertEqual((first["prev_clean"], first["crash"], first["heap_mb"]), (None, False, 40))
        self.assertEqual([d["crash"] for d in self.loads()], [False, True, False])

    def test_tolerates_junk_and_prunes_old(self):
        DisplayLoad.objects.create(ip="192.168.1.99", at=timezone.now() - timedelta(days=8))
        self.send(heap_mb="", prev_lived_s="x", prev_clean="maybe", path="/" * 300)
        [row] = self.loads()
        self.assertEqual((row["heap_mb"], row["prev_lived_s"], row["prev_clean"], len(row["path"])),
                         (None, None, None, 200))
        self.assertEqual(len(self.loads(limit="0")), 1)

    def test_every_page_reports_its_load(self):
        html = self.client.get("/").content.decode()
        self.assertIn(reverse("display_page_load"), html)

    def test_keeps_previous_pages_last_stats(self):
        self.send(prev_clean="0", prev_stats='{"now": {"lag_max_ms": 9000, "stalls": 3}}')
        self.send(prev_clean="0", prev_stats="[1, 2]")
        self.send(prev_clean="0", prev_stats="{" + "x" * 20000)
        self.assertEqual([d["prev_stats"] for d in self.loads()],
                         [None, None, {"now": {"lag_max_ms": 9000, "stalls": 3}}])


class SampleTests(TestCase):
    def send(self, data, ip="192.168.1.99", path="/apps/cameras/"):
        return Client(enforce_csrf_checks=True).post(reverse("display_sample"), {"path": path, "data": data},
                                                     REMOTE_ADDR=ip)

    def samples(self, **params):
        return self.client.get(reverse("display_samples"), params).json()["samples"]

    def test_records_and_lists_newest_first(self):
        self.assertEqual(self.send('{"lag_max_ms": 40}').status_code, 204)
        self.send('{"lag_max_ms": 1200, "app": {"app": "cameras", "data": {"heap_mb": 80}}}')
        self.send('{"lag_max_ms": 5}', ip="192.168.1.50")
        rows = self.samples(ip="192.168.1.99")
        self.assertEqual([r["data"]["lag_max_ms"] for r in rows], [1200, 40])
        self.assertEqual((rows[0]["path"], rows[0]["data"]["app"]["data"]["heap_mb"]), ("/apps/cameras/", 80))
        self.assertEqual(len(self.samples(limit="1")), 1)

    def test_rejects_non_objects_and_prunes_old(self):
        for bad in ("", "nope", "[1]", "{" + "x" * 20000):
            self.assertEqual(self.send(bad).status_code, 400)
        old = DisplaySample.objects.create(ip="192.168.1.99", at=timezone.now() - timedelta(days=4))
        self.send("{}")
        self.assertFalse(DisplaySample.objects.filter(pk=old.pk).exists())
        self.assertEqual(len(self.samples(since=(timezone.now() - timedelta(minutes=1)).isoformat())), 1)

    def test_every_page_sends_samples(self):
        html = self.client.get("/").content.decode()
        self.assertIn(reverse("display_sample"), html)


class CatalogTests(TestCase):
    def test_groups(self):
        with override_settings(HUB_PLUGINS=BASE + (WEATHER,)):
            self.assertEqual(catalog.stat_groups(["weather.temp", "gone.x"]), [
                ("Weather", [("weather.temp", "Outside"), ("weather.feels_like", "Feels like"),
                             ("weather.sun", "Next sunrise or sunset")]),
                ("Unavailable", [("gone.x", "gone.x (unavailable)")]),
            ])
        with override_settings(HUB_PLUGINS=MOUNTAIN):
            groups = dict(catalog.stat_groups())
        self.assertIn(("lift_status.runs_open", "Runs open"), groups["Lift Status"])
        self.assertNotIn("Screensaver", groups)  # days until lifts needs an app that knows the season
        hint = PluginSpec(id="lift_status", app="lift_status", provides={STATS: P + "lift_stats", SEASON: P + "lifts_hint"},
                          stat_keys=(("runs_open", "Runs open"),))
        with override_settings(HUB_PLUGINS=BASE + (SNOW, hint)):
            groups = dict(catalog.stat_groups())
        self.assertEqual(groups["Screensaver"], [("days_until_lifts", "Days until lifts open")])


class ScheduleTests(TestCase):
    def test_is_night_wraps_midnight(self):
        night = (23 * 60, 6 * 60)
        self.assertTrue(is_night(23 * 60, *night))
        self.assertTrue(is_night(3 * 60, *night))
        self.assertFalse(is_night(6 * 60, *night))
        self.assertFalse(is_night(12 * 60, *night))
        self.assertTrue(is_night(14 * 60, 13 * 60, 15 * 60))
        self.assertFalse(is_night(0, 0, 0))  # same start and end: never

    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm(" 6:05 "), 365)
        for bad in ("", "6", "24:00", "12:60", "ab:cd"):
            with self.assertRaises(ValueError):
                parse_hhmm(bad)


class ConnectTests(TestCase):
    """Turning UniFi Connect screens off overnight."""
    SCREEN = {"id": "dev1", "name": "Hub", "extraInfo": {"ethIP": "10.0.0.9"},
              "type": {"supportedActions": [{"id": "a-off", "name": "display_off"},
                                            {"id": "a-on", "name": "display_on"}]}}

    def setUp(self):
        store.clear()
        self.addCleanup(store.clear)
        self.state = FakeState()
        self.state.delete = self.state.pop
        patcher = mock.patch("display.connect.app_state", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        AppSetting.objects.create(app_id="display", key="connect_host", value="10.0.0.1")
        AppSetting.objects.create(app_id="display", key="connect_username", value="hub")
        secret = mock.patch("display.connect.secrets.get", return_value="pw")
        secret.start()
        self.addCleanup(secret.stop)

    def session(self, screens=None, fail=False):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.headers = {}
        session.post.return_value.headers = {"X-Csrf-Token": "tok"}
        session.get.return_value.json.return_value = {"data": [self.SCREEN] if screens is None else screens}
        if fail:
            session.patch.return_value.raise_for_status.side_effect = connect.requests.HTTPError("401 pw=pw")
        return mock.patch("display.connect.requests.Session", return_value=session), session

    def night(self, dark):
        return mock.patch("display.connect.night_now", return_value=dark)

    def test_sleep_turns_the_screen_off_at_night(self):
        patch, session = self.session()
        with patch, self.night(True):
            self.assertTrue(connect.sleep("10.0.0.9"))
        session.post.assert_called_once_with("https://10.0.0.1/api/auth/login", timeout=connect.TIMEOUT,
                                             json={"username": "hub", "password": "pw"})
        self.assertEqual(session.headers["X-Csrf-Token"], "tok")
        session.patch.assert_called_once_with("https://10.0.0.1/proxy/connect/api/v2/devices/dev1/status",
                                              timeout=connect.TIMEOUT, json={"id": "a-off", "name": "display_off"})
        self.assertEqual(self.state[connect.SLEPT], ["10.0.0.9"])

    def test_sleep_does_nothing_by_day_unset_or_unknown_screen(self):
        patch, session = self.session()
        with patch, self.night(False):
            self.assertFalse(connect.sleep("10.0.0.9"))
        with patch, self.night(True):
            self.assertFalse(connect.sleep("10.0.0.50"))  # not a Connect screen
        session.patch.assert_not_called()
        AppSetting.objects.filter(key="connect_host").delete()
        store.clear()
        self.assertFalse(connect.configured())
        with patch, self.night(True):
            self.assertFalse(connect.sleep("10.0.0.9"))
        self.assertNotIn(connect.SLEPT, self.state)

    def test_failure_never_shows_the_password(self):
        patch, _ = self.session(fail=True)
        with patch, self.night(True), self.assertRaises(connect.ConnectError) as err:
            connect.sleep("10.0.0.9")
        self.assertNotIn("pw", str(err.exception))

    def test_wake_in_the_morning_and_retry_failures(self):
        self.state[connect.SLEPT] = ["10.0.0.9"]
        patch, session = self.session()
        with patch, self.night(True):
            self.assertEqual(connect.wake_due(), [])  # still night
        session.patch.assert_not_called()
        failing, _ = self.session(fail=True)
        with failing, self.night(False):
            self.assertEqual(connect.wake_due(), [])
        self.assertEqual(self.state[connect.SLEPT], ["10.0.0.9"])  # tried again next minute
        with patch, self.night(False):
            self.assertEqual(connect.wake_due(), ["10.0.0.9"])
        session.patch.assert_called_once_with("https://10.0.0.1/proxy/connect/api/v2/devices/dev1/status",
                                              timeout=connect.TIMEOUT, json={"id": "a-on", "name": "display_on"})
        self.assertNotIn(connect.SLEPT, self.state)

    def test_sleep_endpoint(self):
        url = reverse("display_sleep")
        self.assertEqual(self.client.get(url).status_code, 405)
        with mock.patch("display.connect.sleep", return_value=True) as sleep:
            self.assertEqual(self.client.post(url, REMOTE_ADDR="10.0.0.9").json(), {"asleep": True})
        sleep.assert_called_once_with("10.0.0.9")
        with mock.patch("display.connect.sleep", side_effect=connect.ConnectError("boom")):
            self.assertEqual(self.client.post(url).json(), {"asleep": False})

    def test_saver_asks_to_sleep_only_when_set_up(self):
        from .templatetags.display_tags import screensaver
        self.assertEqual(screensaver()["config"]["sleep"], reverse("display_sleep"))
        AppSetting.objects.filter(key="connect_host").delete()
        store.clear()
        self.assertNotIn("sleep", screensaver()["config"])

    def test_settings_page_keeps_the_password_secret(self):
        from hub_settings import vault
        vault.set_provider(vault.StaticKeyProvider(("k1", b"\x07" * 32)))
        self.addCleanup(vault.set_provider, None)
        self.staff = get_user_model().objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        page = self.client.get(reverse("display_settings"))
        self.assertContains(page, 'name="connect_host" value="10.0.0.1"')
        self.assertContains(page, "Not set")
        data = ViewTests.form(self, connect_password="s3cret-pass")
        self.client.post(reverse("display_settings"), data)
        self.assertEqual(vault.get_secret("display", "connect_password"), "s3cret-pass")
        self.client.post(reverse("display_settings"), {**data, "connect_password": ""})  # blank keeps it
        self.assertEqual(vault.get_secret("display", "connect_password"), "s3cret-pass")
        page = self.client.get(reverse("display_settings"))
        self.assertContains(page, "Saved; leave blank to keep it")
        self.assertNotContains(page, "s3cret-pass")
        self.assertNotIn("connect_password", page.context["v"])


def jpeg(size=(800, 600), orientation=None, name="p.jpg", taken=None):
    img = Image.new("RGB", size, (200, 30, 30))
    buf = io.BytesIO()
    exif = img.getexif()
    if orientation:
        exif[0x0112] = orientation
    if taken:
        exif[0x8769] = {36867: taken}  # Exif IFD: DateTimeOriginal
    exif[0x010F] = "SecretCam"  # Make: must not survive processing
    img.save(buf, "JPEG", exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


@override_settings(HUB_PLUGINS=BASE)  # no live providers
class PhotoTests(TestCase):
    def setUp(self):
        self.addCleanup(store.clear)
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        storage = FileSystemStorage(root, base_url="/media/apps/display/")
        for name in ("image", "thumb"):
            patcher = mock.patch.object(SaverPhoto._meta.get_field(name), "storage", storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root
        User = get_user_model()
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.guest = User.objects.create_user("guest", password="pw")
        self.client.force_login(self.staff)

    def link(self):
        return self.client.post(reverse("display_upload_link")).json()

    def test_phone_upload_flow(self):
        link = self.link()
        self.assertIn("<svg", link["qr"])
        self.assertTrue(link["url"].endswith(f"/display/upload/{link['key']}/"))
        phone = Client()
        page = phone.get(reverse("display_upload", args=[link["key"]]))
        self.assertContains(page, "Choose photos")
        self.assertEqual(page["Cache-Control"], "no-store")
        response = phone.post(reverse("display_upload_photo", args=[link["key"]]),
                              {"photo": jpeg(), "seasons": "summer"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["photos"][0]["tag"], "Summer")
        status = self.client.get(reverse("display_photos"), {"link": link["key"]}).json()
        self.assertEqual(status["link"]["arrived"], 1)
        self.assertTrue(status["link"]["claimed"])
        self.assertGreater(status["link"]["seconds"], 25 * 60)  # claiming extends the code

        other = Client()
        self.assertEqual(other.get(reverse("display_upload", args=[link["key"]])).status_code, 403)
        self.assertEqual(other.post(reverse("display_upload_photo", args=[link["key"]]),
                                    {"photo": jpeg()}).status_code, 403)

    def test_upload_link_errors(self):
        phone = Client()
        self.assertEqual(phone.get(reverse("display_upload", args=["nope"])).status_code, 404)
        link = UploadLink.objects.create()
        # Posting without opening the page (no claim) is refused.
        self.assertEqual(phone.post(reverse("display_upload_photo", args=[link.key]),
                                    {"photo": jpeg()}).status_code, 403)
        UploadLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(phone.get(reverse("display_upload", args=[link.key])), "expired", status_code=410)

    def test_processing(self):
        response = self.client.post(reverse("display_photo_add"), {"photo": jpeg((4000, 1000), orientation=6)})
        self.assertEqual(response.status_code, 200)
        photo = SaverPhoto.objects.get()
        self.assertEqual(photo.seasons, [])
        with Image.open(photo.image.path) as img:
            self.assertEqual(img.size, (640, 2560))  # rotated upright, then fit to 2560
            self.assertNotIn(0x010F, img.getexif())
        with Image.open(photo.thumb.path) as img:
            self.assertLessEqual(max(img.size), 480)

        bad = SimpleUploadedFile("x.jpg", b"not an image", content_type="image/jpeg")
        self.assertEqual(self.client.post(reverse("display_photo_add"), {"photo": bad}).status_code, 400)

    def test_update_and_delete(self):
        self.client.post(reverse("display_photo_add"), {"photo": jpeg(), "seasons": "winter,spring,summer,fall"})
        photo = SaverPhoto.objects.get()
        self.assertEqual(photo.seasons, [])  # all four means any season
        url = reverse("display_photo_update", args=[photo.id])
        body = self.client.post(url, {"seasons": "fall,winter", "enabled": "0"}).json()
        self.assertEqual(body["seasons"], ["fall", "winter"])
        self.assertFalse(body["enabled"])
        path = photo.image.path
        self.client.post(reverse("display_photo_delete", args=[photo.id]))
        self.assertFalse(SaverPhoto.objects.exists())
        self.assertFalse(os.path.exists(path))

    def test_staff_only(self):
        self.client.post(reverse("display_photo_add"), {"photo": jpeg()})
        photo = SaverPhoto.objects.get()
        self.client.force_login(self.guest)
        for url in (reverse("display_upload_link"), reverse("display_photo_add"),
                    reverse("display_photo_update", args=[photo.id]),
                    reverse("display_photo_delete", args=[photo.id])):
            self.assertEqual(self.client.post(url, {"enabled": "0"}).status_code, 302, url)
        self.assertEqual(self.client.get(reverse("display_photos")).status_code, 302)
        self.assertTrue(SaverPhoto.objects.get().enabled)

    def test_saver_api_photos(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="photos")
        AppSetting.objects.create(app_id="display", key="source_winter", value="photos")
        self.client.post(reverse("display_photo_add"), {"photo": jpeg(), "seasons": "summer"})
        summer = self.client.get(reverse("display_saver_api"), {"season": "summer"}).json()
        self.assertEqual(summer["sources"], ["photos"])
        self.assertEqual(summer["photos"], [{"url": SaverPhoto.objects.get().image.url, "caption": ""}])
        winter = self.client.get(reverse("display_saver_api"), {"season": "winter"}).json()
        self.assertEqual(winter["sources"], ["stock"])  # no winter photos: fall back to the scene
        SaverPhoto.objects.update(enabled=False)
        summer = self.client.get(reverse("display_saver_api"), {"season": "summer"}).json()
        self.assertEqual(summer["sources"], ["stock"])

    def test_caption_from_exif_date(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="photos")
        self.client.post(reverse("display_photo_add"), {"photo": jpeg(taken="2024:03:31 23:30:00")})
        self.client.post(reverse("display_photo_add"), {"photo": jpeg(name="q.jpg")})
        dated = SaverPhoto.objects.exclude(taken_at=None).get()
        self.assertEqual((dated.taken_at.year, dated.taken_at.month, dated.taken_at.day), (2024, 3, 31))
        api = reverse("display_saver_api")
        off = self.client.get(api, {"season": "summer"}).json()["photos"]
        self.assertEqual({p["caption"] for p in off}, {""})
        AppSetting.objects.create(app_id="display", key="photo_caption", value=True)
        store.clear()
        on = self.client.get(api, {"season": "summer"}).json()["photos"]
        self.assertEqual(sorted(p["caption"] for p in on), ["", "March 2024"])

    def test_preview_overrides_are_staff_only(self):
        self.client.post(reverse("display_photo_add"), {"photo": jpeg(taken="2023:12:25 08:00:00")})
        api = reverse("display_saver_api")
        preview = {"season": "summer", "source": "photos", "rotate": "9999", "caption": "1"}
        body = self.client.get(api, preview).json()
        self.assertEqual(body["sources"], ["photos"])
        self.assertEqual(body["rotate_seconds"], 90)  # clamped to the slider's range
        self.assertEqual(self.client.get(api, dict(preview, rotate="5")).json()["rotate_seconds"], 15)
        self.assertEqual(body["photos"][0]["caption"], "December 2023")
        self.client.force_login(self.guest)
        body = self.client.get(api, preview).json()
        self.assertEqual(body["sources"], ["stock"])
        self.assertEqual(body["photos"], [])

@tag("selenium")
@override_settings(HUB_PLUGINS=BASE)
class SaverBrokenPhotoSeleniumTest(SeleniumTestCase):
    """A photo that fails to load must not leave the screensaver black (regression)."""

    GOOD = "android-chrome-512x512.png"  # a real file in static/, served by the live server

    def setUp(self):
        super().setUp()
        self.addCleanup(store.clear)
        storage = FileSystemStorage(os.path.join(settings.BASE_DIR, "static"), base_url=settings.STATIC_URL)
        for name in ("image", "thumb"):
            patcher = mock.patch.object(SaverPhoto._meta.get_field(name), "storage", storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        AppSetting.objects.create(app_id="display", key="source_summer", value="photos")
        self.browser.get("about:blank")

    def add(self, name):
        SaverPhoto.objects.create(image=name, thumb=name)

    def open_saver(self):
        self.browser.get(self.live_server_url + "/?saver=1&saver_season=summer")

    def layer(self, name):
        return self.browser.find_element(By.CSS_SELECTOR, f".bdh-saver .layer.{name}")

    def test_only_broken_photos_fall_back_to_stock(self):
        self.add("missing-1.jpg")
        self.add("missing-2.jpg")
        self.open_saver()
        # Wait for the photos to have been tried and the saver to have fallen back.
        WebDriverWait(self.browser, 20).until(lambda d: (
            d.find_element(By.CSS_SELECTOR, ".bdh-saver .clock").get_attribute("textContent")
            and "on" in self.layer("stock").get_attribute("class")
            and "on" not in self.layer("photos").get_attribute("class")))

    def test_season_name_is_not_shown(self):
        self.open_saver()
        WebDriverWait(self.browser, 20).until(
            lambda d: d.find_element(By.CSS_SELECTOR, ".bdh-saver .clock").get_attribute("textContent"))
        text = self.browser.find_element(By.CSS_SELECTOR, ".bdh-saver").get_attribute("textContent").lower()
        self.assertNotIn("summer", text)

    def test_stock_rotates_season_paintings(self):
        self.open_saver()  # no photos yet: the stock layer shows
        frame = (By.CSS_SELECTOR, ".bdh-saver .layer.stock.on .frame.in")
        WebDriverWait(self.browser, 20).until(EC.presence_of_element_located(frame))
        self.assertIn("/display/stock/summer/", self.browser.find_element(*frame).get_attribute("data-src"))
        self.assertTrue(self.browser.find_elements(By.CSS_SELECTOR, ".bdh-saver .layer.stock > svg"))

    def test_broken_photos_are_skipped(self):
        for n in range(3):
            self.add(f"missing-{n}.jpg")
        self.add(self.GOOD)
        self.open_saver()
        frame = (By.CSS_SELECTOR, ".bdh-saver .layer.photos.on .frame")
        WebDriverWait(self.browser, 20).until(EC.presence_of_element_located(frame))
        self.assertTrue(self.browser.find_element(*frame).get_attribute("data-src").endswith(self.GOOD))

    def test_photos_saver_never_loads_stock(self):
        """Regression: a stock painting used to flash up before the photos took over."""
        self.add(self.GOOD)
        self.open_saver()
        frame = (By.CSS_SELECTOR, ".bdh-saver .layer.photos.on .frame")
        WebDriverWait(self.browser, 20).until(EC.presence_of_element_located(frame))
        self.assertNotIn("on", self.layer("stock").get_attribute("class").split())
        self.assertFalse(self.browser.find_elements(By.CSS_SELECTOR, ".bdh-saver .layer.stock .frame"))

    def test_fades_in_only_once_the_first_photo_is_there(self):
        """It used to fade in black with just the clock, then the photo arrived."""
        self.add(self.GOOD)
        self.browser.get(self.live_server_url + "/?saver_season=summer")
        self.browser.execute_script("""
            new MutationObserver((_, obs) => {
                const root = document.querySelector('.bdh-saver.on');
                if (!root) return;
                window.saverOnWithPhoto = !!root.querySelector('.layer.photos.on .frame');
                obs.disconnect();
            }).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['class']});
            window.BDHSaver.show();
        """)
        WebDriverWait(self.browser, 20).until(lambda d: d.execute_script("return 'saverOnWithPhoto' in window"))
        self.assertTrue(self.browser.execute_script("return window.saverOnWithPhoto"))


@tag("selenium")
@override_settings(HUB_PLUGINS=BASE)
class DisplaySettingsSeleniumTest(SeleniumTestCase):
    """Settings → Display in a real browser (regressions)."""

    GOOD = "android-chrome-512x512.png"

    def setUp(self):
        super().setUp()
        self.addCleanup(store.clear)
        storage = FileSystemStorage(os.path.join(settings.BASE_DIR, "static"), base_url=settings.STATIC_URL)
        for name in ("image", "thumb"):
            patcher = mock.patch.object(SaverPhoto._meta.get_field(name), "storage", storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        get_user_model().objects.create_superuser("testuser", "t@example.com", "testpassword")
        login_via_browser(self.browser, self.live_server_url)

    def open(self):
        self.browser.get(self.live_server_url + reverse("display_settings") + "?season=summer")
        WebDriverWait(self.browser, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#dsp-prev img"))

    def preview_src(self):
        return self.browser.find_element(By.CSS_SELECTOR, "#dsp-prev img").get_attribute("src")

    def test_my_photos_preview_shows_a_library_photo(self):
        SaverPhoto.objects.create(image=self.GOOD, thumb=self.GOOD)
        AppSetting.objects.create(app_id="display", key="source_summer", value="photos")
        self.open()
        self.assertTrue(self.preview_src().endswith(self.GOOD))
        self.browser.find_element(By.CSS_SELECTOR, 'input[name="source_summer"][value="stock"]').click()
        WebDriverWait(self.browser, 5).until(lambda d: "/display/stock/summer/" in self.preview_src())
        self.browser.find_element(By.CSS_SELECTOR, 'input[name="source_summer"][value="photos"]').click()
        WebDriverWait(self.browser, 5).until(lambda d: self.preview_src().endswith(self.GOOD))

    def test_my_photos_preview_falls_back_to_paintings(self):
        AppSetting.objects.create(app_id="display", key="source_summer", value="photos")
        self.open()
        self.assertIn("/display/stock/summer/", self.preview_src())

    def test_no_scrollbar_and_a_scroll_hint(self):
        self.browser.set_window_size(1280, 800)
        self.open()
        b = self.browser
        scrollers = "[document.documentElement, document.body]"
        self.assertTrue(b.execute_script(f"return {scrollers}.some(e => e.scrollHeight > e.clientHeight)"))
        self.assertEqual(b.execute_script(f"return {scrollers}.map(e => e.offsetWidth - e.clientWidth)"), [0, 0])
        hint = (By.CSS_SELECTOR, ".hub-scroll-hint")
        WebDriverWait(b, 5).until(lambda d: "on" in d.find_element(*hint).get_attribute("class").split())
        b.execute_script(f"{scrollers}.forEach(e => e.scrollTop = e.scrollHeight)")
        WebDriverWait(b, 5).until(lambda d: "on" not in d.find_element(*hint).get_attribute("class").split())

    def test_my_photos_open_in_the_viewer(self):
        SaverPhoto.objects.create(image=self.GOOD, thumb=self.GOOD)
        self.open()
        b = self.browser
        b.find_element(By.CSS_SELECTOR, "#dsp-lib-grid .dsp-ph[data-id] img").click()
        viewer = b.find_element(By.ID, "dsp-view")
        WebDriverWait(b, 5).until(lambda d: viewer.is_displayed())
        self.assertTrue(b.find_element(By.ID, "dsp-view-img").get_attribute("src").endswith(self.GOOD))
        self.assertFalse(b.find_element(By.CSS_SELECTOR, "#dsp-view .dsp-step.next").is_displayed())
        b.find_element(By.CSS_SELECTOR, "#dsp-view [data-close]").click()
        WebDriverWait(b, 5).until(lambda d: not viewer.is_displayed())

    def test_built_in_paintings_tab_and_viewer(self):
        self.open()
        b = self.browser
        b.find_element(By.CSS_SELECTOR, '[data-lib="stock"]').click()
        tiles = b.find_elements(By.CSS_SELECTOR, "#dsp-stock-grid .dsp-art")
        extras = sum(len(STOCK_EXTRAS.get(s, ())) for s, _ in SEASONS)
        self.assertEqual(len(tiles), len(STOCK_STYLES) * len(SEASONS) + extras)
        self.assertFalse(b.find_element(By.ID, "dsp-lib-grid").is_displayed())
        tiles[0].click()
        viewer = b.find_element(By.ID, "dsp-view")
        WebDriverWait(b, 5).until(lambda d: viewer.is_displayed())
        first = b.find_element(By.ID, "dsp-view-img").get_attribute("src")
        b.find_element(By.CSS_SELECTOR, "#dsp-view .dsp-step.next").click()
        self.assertNotEqual(b.find_element(By.ID, "dsp-view-img").get_attribute("src"), first)
        b.find_element(By.CSS_SELECTOR, "#dsp-view [data-close]").click()
        WebDriverWait(b, 5).until(lambda d: not viewer.is_displayed())
        b.find_element(By.CSS_SELECTOR, '[data-lib="mine"]').click()
        self.assertTrue(b.find_element(By.ID, "dsp-add").is_displayed())


@tag("selenium")
@override_settings(HUB_PLUGINS=BASE + (HILL,))
class AppSaverSeleniumTest(SeleniumTestCase):
    """An app's live source on the screen saver itself."""

    def setUp(self):
        super().setUp()
        self.addCleanup(store.clear)
        AppSetting.objects.create(app_id="display", key="source_summer", value="hillcams.cams")
        AppSetting.objects.create(app_id="display", key="rotate_seconds", value=15)
        self.browser.get("about:blank")

    def shown(self):
        return self.browser.execute_script("""
            const img = document.querySelector('.bdh-saver .layer.cam.on img');
            return img && [img.dataset.src, document.querySelector('.bdh-saver .cam-name').textContent];
        """)

    def test_live_source_rotates_through_its_cameras(self):
        self.browser.get(self.live_server_url + "/?saver=1&saver_season=summer")
        WebDriverWait(self.browser, 20).until(lambda d: self.shown())
        self.assertEqual(self.shown(), ["/static/android-chrome-512x512.png", "Cam A"])
        WebDriverWait(self.browser, 40).until(lambda d: (self.shown() or [""])[0].endswith("192x192.png"))
        self.assertEqual(self.shown()[1], "Cam B")

    def test_picked_camera_stays(self):
        AppSetting.objects.create(app_id="display", key="saver_pick", value="hillcams.cams/b")
        self.browser.get(self.live_server_url + "/?saver=1&saver_season=summer")
        WebDriverWait(self.browser, 20).until(lambda d: self.shown())
        self.assertEqual(self.shown(), ["/static/android-chrome-192x192.png", "Cam B"])
        self.assertEqual(len(self.browser.find_elements(By.CSS_SELECTOR, ".bdh-saver .layer.cam")), 1)


@tag("selenium")
@override_settings(HUB_PLUGINS=BASE + (HILL, DECK, GALLERY))
class AppSourceSettingsSeleniumTest(SeleniumTestCase):
    """Settings → Display with screen saver sources from apps."""

    def setUp(self):
        super().setUp()
        self.addCleanup(store.clear)
        get_user_model().objects.create_superuser("testuser", "t@example.com", "testpassword")
        login_via_browser(self.browser, self.live_server_url)
        self.browser.get(self.live_server_url + reverse("display_settings") + "?season=summer")
        WebDriverWait(self.browser, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#dsp-prev img"))

    def pick(self, source):
        self.browser.find_element(By.CSS_SELECTOR, f'input[name="source_summer"][value="{source}"]').click()

    def visible(self, css):
        return [e for e in self.browser.find_elements(By.CSS_SELECTOR, css) if e.is_displayed()]

    def shows(self):
        b = self.browser
        return {"picker": bool(self.visible("[data-pick-for]")),
                "rotate": b.find_element(By.ID, "dsp-rot-row").is_displayed(),
                "caption": b.find_element(By.ID, "dsp-caption-row").is_displayed()}

    def preview_src(self):
        img = self.browser.find_elements(By.CSS_SELECTOR, "#dsp-prev img")
        return img[0].get_attribute("src") if img else ""

    def test_cards_in_order_with_the_apps_words(self):
        cards = self.visible('.dsp-src[data-season="summer"] label[data-src]')
        self.assertEqual([c.get_attribute("data-src") for c in cards],
                         ["stock", "photos", "hillcams.cams", "deckcam.cam", "gallery.shots", "mix"])
        self.assertIn("Hill cams", cards[2].text)
        self.assertIn("Two cameras up the hill", cards[2].text)
        self.assertIn("Paintings, photos, Hill cams, Deck cam & Lodge shots", cards[-1].text)

    def test_controls_follow_the_chosen_source(self):
        self.assertEqual(self.shows(), {"picker": False, "rotate": True, "caption": False})  # stock
        self.pick("hillcams.cams")
        self.assertEqual(self.shows(), {"picker": True, "rotate": True, "caption": False})
        Select(self.browser.find_element(By.CSS_SELECTOR, '[data-pick-for="hillcams.cams"]')).select_by_visible_text(
            "Cam B")
        self.assertEqual(self.browser.find_element(By.ID, "dsp-pick").get_attribute("value"), "hillcams.cams/b")
        self.assertEqual(self.shows(), {"picker": True, "rotate": False, "caption": False})  # one camera picked
        WebDriverWait(self.browser, 5).until(lambda d: self.preview_src().endswith("192x192.png"))
        self.pick("deckcam.cam")
        self.assertEqual(self.shows(), {"picker": False, "rotate": False, "caption": False})  # a single camera
        self.pick("gallery.shots")
        self.assertEqual(self.shows(), {"picker": False, "rotate": True, "caption": True})
        self.pick("mix")
        self.assertEqual(self.shows(), {"picker": False, "rotate": True, "caption": True})

    def overflow(self):
        """Visible things in the screen saver card that stick out past its right edge."""
        return self.browser.execute_script("""
            const card = document.querySelector('.dsp-saver'), edge = card.getBoundingClientRect().right + 1;
            return [...card.querySelectorAll('*')].filter(e => !e.closest('.dsp-prev') && e.getClientRects().length
                && e.getBoundingClientRect().right > edge)
                .map(e => e.tagName + (e.id ? '#' + e.id : '') + '.' + e.className + ' ' + (e.textContent || '').trim().slice(0, 30));
        """)

    def test_nothing_sticks_out_of_the_card(self):
        self.addCleanup(self.browser.set_window_size, 1920, 1080)
        for width in (1920, 1400, 1280, 1024, 800, 600):
            self.browser.set_window_size(width, 1080)
            for source in ("stock", "photos", "hillcams.cams", "deckcam.cam", "gallery.shots", "mix"):
                self.pick(source)
                with self.subTest(width=width, source=source):
                    self.assertEqual(self.overflow(), [])

    def test_preview_shows_the_live_picture(self):
        self.assertIn("/display/stock/summer/", self.preview_src())
        self.pick("hillcams.cams")
        WebDriverWait(self.browser, 5).until(lambda d: self.preview_src().endswith("/static/android-chrome-512x512.png"))
        self.assertEqual(self.browser.find_element(By.ID, "dsp-prev-tag").text, "Hill cams \u00b7 Cam A (rotating)")
        WebDriverWait(self.browser, 5).until(lambda d: not d.find_element(By.ID, "dsp-prev-wake").is_displayed())
        self.pick("deckcam.cam")
        WebDriverWait(self.browser, 5).until(lambda d: "Deck cam" in d.find_element(By.ID, "dsp-prev-tag").text)

