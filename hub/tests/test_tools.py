"""The assistant's tool gateway (hub.tools) and its broker ops."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from bdh_sdk import events as bus
from bdh_sdk.tools import Tool, ToolError, tool_name
from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings

from broker.models import AuditEntry
from broker.tests.base import auth, make_install, manifest, reset_broker
from hub import tools
from hub.plugins import (
    ALL_MODES, DASHBOARD, GUEST, OWNER, OWNER_ONLY, Action, PluginSpec, Reading, Shortcut, Status,
    StatusRow, Stay, Tile,
)
from hub_settings import store as hub_store
from hub_settings.models import AppSetting

STATE = {}
LONG_ID = "a" * 30
LONG_TOOL = "b" * 40


def _tile(title, modes=ALL_MODES):
    return Tile(title=title, url_name="hub_settings_location", icon="wifi/hub/icon.svg", section="more",
                modes=modes)


def lights_status():
    return Status("3 lights on", readings=(Reading("Inside", "21", "°C"),),
                  actions=(Action("all_off", "fa-solid fa-power-off", label="All off"),))


def lights_shortcuts():
    return [Shortcut(id="movie", title="Movie night", icon="fa-solid fa-film", modes=ALL_MODES),
            Shortcut(id="lock_up", title="Lock up", icon="fa-solid fa-lock")]


def lights_run_shortcut(shortcut_id):
    STATE.setdefault("ran", []).append(shortcut_id)
    return "Enjoy the show"


def lights_run_action(row_id, action_id):
    STATE.setdefault("acted", []).append(("lights", row_id, action_id))
    return "All off"


def alarm_status():
    return Status("Disarmed", rows=(StatusRow(id="house", title="House",
                                              actions=(Action("arm", "fa-solid fa-lock", label="Arm"),)),))


def alarm_run_action(row_id, action_id):
    STATE.setdefault("acted", []).append(("alarm", row_id, action_id))
    return "Armed"


def heat_tools(mode):
    STATE.setdefault("listed", []).append(mode)
    return [
        Tool("get_temp", "Room temperature", "The temperature in a room.", readonly=True, modes=ALL_MODES,
             params={"type": "object", "properties": {"room": {"type": "string", "enum": ["den", "loft"]}},
                     "required": ["room"]}),
        Tool("set_temp", "Set temperature", "Set the thermostat.", modes={OWNER, GUEST, DASHBOARD},
             params={"type": "object",
                     "properties": {"degrees": {"type": "integer", "minimum": 10, "maximum": 30}},
                     "required": ["degrees"]}),
        Tool("unlock", "Unlock the door", "Unlock the front door."),
        Tool("fail", "Fail", "Always reports a failure.", modes=ALL_MODES),
        Tool("crash", "Crash", "Always crashes.", modes=ALL_MODES),
        Tool("slow", "Slow", "Takes its time.", modes=ALL_MODES),
    ]


def heat_run(tool_id, args, mode):
    STATE.setdefault("calls", []).append((tool_id, args, mode))
    if tool_id == "fail":
        raise ToolError("the heater is offline")
    if tool_id == "crash":
        raise RuntimeError("secret internals")
    if tool_id == "slow":
        STATE["release"].wait(5)
    if tool_id == "get_temp":
        return {"room": args["room"], "celsius": 21}
    return None


def long_tools():
    return [Tool(LONG_TOOL, "Long", "A tool with a long name.", modes=ALL_MODES)]


def long_run(tool_id, args):
    STATE.setdefault("calls", []).append((tool_id, args, None))
    return "long done"


def demo_stay():
    return STATE.get("stay")


PLUGINS = (
    PluginSpec(id="lights", app="lights", tiles=(_tile("Lights"),),
               status="hub.tests.test_tools.lights_status", run_action="hub.tests.test_tools.lights_run_action",
               shortcuts="hub.tests.test_tools.lights_shortcuts",
               run_shortcut="hub.tests.test_tools.lights_run_shortcut"),
    PluginSpec(id="alarm", app="alarm", tiles=(_tile("Alarm", OWNER_ONLY),),
               status="hub.tests.test_tools.alarm_status", run_action="hub.tests.test_tools.alarm_run_action"),
    PluginSpec(id="heat", app="heat", tiles=(_tile("Heat"),),
               tools="hub.tests.test_tools.heat_tools", run_tool="hub.tests.test_tools.heat_run"),
    PluginSpec(id=LONG_ID, app=LONG_ID, tools="hub.tests.test_tools.long_tools",
               run_tool="hub.tests.test_tools.long_run"),
    PluginSpec(id="stays", app="stays", stay="hub.tests.test_tools.demo_stay"),
)

LODGE_TZ = timezone(timedelta(hours=-7))


class Recorder:
    def __init__(self, test):
        self.events = []
        sub = bus.subscribe("assistant/#", lambda e: self.events.append((e.topic, e.data)))
        test.addCleanup(sub.cancel)

    def get(self):
        bus.flush()
        return self.events


@override_settings(HUB_PLUGINS=PLUGINS)
class GatewayTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        hub_store.clear()
        self.addCleanup(hub_store.clear)
        STATE.clear()
        reset_broker()
        self.addCleanup(reset_broker)
        tools.forget()
        self.addCleanup(tools.forget)
        self.allow("lights", "alarm", "heat", LONG_ID, "stays")

    def allow(self, *app_ids):
        AppSetting.objects.update_or_create(app_id="hub", key="assistant_apps", defaults={"value": list(app_ids)})
        hub_store.clear()
        cache.clear()

    def start_stay(self, owner=False):
        now = datetime.now(LODGE_TZ)
        STATE["stay"] = Stay("The Smiths", now - timedelta(days=1), now + timedelta(days=2), owner=owner)
        cache.clear()

    def end_stay(self):
        STATE["stay"] = None
        cache.clear()

    def names(self, mode):
        return set(tools.catalog(mode))

    def session(self, app="voice"):
        return tools.load_session(tools.open_session(app, "voice")["token"], app)


class CatalogTests(GatewayTestCase):
    def test_owner_gets_everything(self):
        self.assertEqual(self.names(OWNER), {
            "hub__status", "heat__get_temp", "heat__set_temp", "heat__unlock", "heat__fail", "heat__crash",
            "heat__slow", "lights__shortcut__movie", "lights__shortcut__lock_up", "lights__action____all_off",
            "alarm__action__house__arm", tool_name(LONG_ID, LONG_TOOL)})

    def test_guest_and_dashboard_never_see_owner_only_tools(self):
        for mode in (GUEST, DASHBOARD):
            names = self.names(mode)
            self.assertIn("heat__get_temp", names)
            self.assertIn("lights__shortcut__movie", names)
            self.assertIn("lights__action____all_off", names)
            # The app's owner-only tool, an owner-only shortcut, a button on an owner-only tile.
            for owner_only in ("heat__unlock", "lights__shortcut__lock_up", "alarm__action__house__arm"):
                self.assertNotIn(owner_only, names, mode)

    def test_tools_hook_gets_the_mode(self):
        tools.catalog(GUEST)
        self.assertEqual(STATE["listed"], [GUEST])

    def test_app_hidden_in_a_mode_offers_no_tools_there(self):
        plugins = (PluginSpec(id="heat", app="heat", tiles=(_tile("Heat", OWNER_ONLY),),
                              tools="hub.tests.test_tools.heat_tools", run_tool="hub.tests.test_tools.heat_run"),)
        with override_settings(HUB_PLUGINS=plugins):
            self.assertEqual(self.names(GUEST), {"hub__status"})
            self.assertIn("heat__get_temp", self.names(OWNER))

    def test_quick_bar_prefs_hide_shortcuts(self):
        AppSetting.objects.create(app_id="hub", key="quick", value={"modes": {"lights": [OWNER]}})
        hub_store.clear()
        self.assertNotIn("lights__shortcut__movie", self.names(GUEST))
        self.assertIn("lights__shortcut__movie", self.names(OWNER))

    def test_listing_shape(self):
        entry = next(t for t in tools.listing(GUEST) if t["name"] == "heat__get_temp")
        self.assertEqual(entry, {
            "name": "heat__get_temp", "title": "Room temperature", "description": "The temperature in a room.",
            "parameters": {"type": "object", "properties": {"room": {"type": "string", "enum": ["den", "loft"]}},
                           "required": ["room"], "additionalProperties": False},
            "readonly": True, "confirm": False})
        shortcut = next(t for t in tools.listing(GUEST) if t["name"] == "lights__shortcut__movie")
        self.assertIn("Movie night", shortcut["description"])
        self.assertFalse(shortcut["readonly"])

    def test_names_are_valid_function_names(self):
        for name in tools.catalog(OWNER):
            self.assertRegex(name, r"^[a-zA-Z0-9_-]{1,64}$")

    def test_long_name_round_trips(self):
        name = tool_name(LONG_ID, LONG_TOOL)
        self.assertLessEqual(len(name), 64)
        self.assertIn(name, self.names(GUEST))
        answer = tools.call(self.session(), name, {})
        self.assertEqual(answer, {"ok": True, "result": "long done"})
        self.assertEqual(STATE["calls"], [(LONG_TOOL, {}, None)])

    def test_colliding_names_get_distinct_names(self):
        out = {}
        tools._add(out, tools._entry("a__b", "A", "a", ["tool", "a", "b"]))
        tools._add(out, tools._entry("a__b", "B", "b", ["shortcut", "a", "b"]))
        self.assertEqual(len(out), 2)
        self.assertEqual(out["a__b"]["title"], "A")
        for name in out:
            self.assertRegex(name, r"^[a-zA-Z0-9_-]{1,64}$")

    def test_bad_tools_from_a_container_app_are_dropped(self):
        from broker.remote import Converted

        good = {"id": "ok", "title": "OK", "description": "Fine.", "params": {}, "modes": ["guest"],
                "confirm": False, "readonly": True}
        value = tools._tools_value(Converted([good, {**good, "id": "Bad Id"}, {**good, "id": "x", "modes": []},
                                              {**good, "id": "y", "params": {"type": "array"}}, "junk"]))
        self.assertEqual([t["id"] for t in value], ["ok"])


class DashboardControlTests(GatewayTestCase):
    def test_off_leaves_dashboard_with_readonly_tools(self):
        AppSetting.objects.create(app_id="hub", key="assistant_dashboard_control", value=False)
        hub_store.clear()
        self.assertEqual(self.names(DASHBOARD), {"hub__status", "heat__get_temp"})
        answer = tools.call(self.session(), "heat__set_temp", {"degrees": 20})
        self.assertFalse(answer["ok"])
        self.assertNotIn("calls", STATE)
        # Guests and owners are unaffected.
        self.assertIn("heat__set_temp", self.names(GUEST))
        self.assertIn("heat__set_temp", self.names(OWNER))

    def test_on_by_default(self):
        self.assertTrue(tools.dashboard_control())
        self.assertIn("heat__set_temp", self.names(DASHBOARD))


# Many apps whose tools hooks can be made slow or failing, like on-demand
# container apps that must be woken first.
MANY = [f"app{i:02d}" for i in range(20)]
SLOW = {}


def _many_tools(app_id):
    def hook(mode):
        plan = SLOW.get(app_id)
        if plan == "fail":
            raise RuntimeError("app is restarting")
        if plan is not None:
            plan.wait(10)
        return [Tool("ask", "Ask", f"Ask {app_id}.", readonly=True, modes=ALL_MODES)]
    return hook


def _many_run(tool_id, args):
    return "ok"


for _id in MANY:
    globals()[f"{_id}_tools"] = _many_tools(_id)

MANY_PLUGINS = tuple(
    PluginSpec(id=i, app=i, tools=f"hub.tests.test_tools.{i}_tools", run_tool="hub.tests.test_tools._many_run")
    for i in MANY)


@override_settings(HUB_PLUGINS=MANY_PLUGINS)
class SlowToolsTests(GatewayTestCase):
    """A listing must never lose an app's tools to a slow refresh (2026-11-27:
    seven sleeping apps' tools hooks timed out together after a restart, and a
    whole voice conversation ran without their tools)."""

    def setUp(self):
        super().setUp()
        self.allow(*MANY)
        SLOW.clear()
        self.addCleanup(SLOW.clear)
        self.gate = threading.Event()
        self.addCleanup(self.drain)

    def drain(self):
        self.gate.set()
        from concurrent.futures import wait

        from hub import status

        wait(list(status._inflight.values()), timeout=10)

    def slow(self, *app_ids):
        for app_id in app_ids or MANY:
            SLOW[app_id] = self.gate

    def apps_listed(self, mode=GUEST, timeout=tools.LIST_TIMEOUT):
        return {name.split("__")[0] for name in tools.catalog(mode, timeout) if name != "hub__status"}

    def saturate_shared_pool(self):
        from hub import status

        for _ in range(status._executor._max_workers * 2):
            status._executor.submit(self.gate.wait, 10)

    def test_listed_once_every_app_stays_listed(self):
        self.saturate_shared_pool()
        self.assertEqual(self.apps_listed(), set(MANY))
        # The cache loses everything (expiry, culling); every refresh is slow and a few fail.
        cache.clear()
        self.slow()
        SLOW["app03"] = SLOW["app11"] = "fail"
        started = time.monotonic()
        self.assertEqual(self.apps_listed(timeout=2.0), set(MANY))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_expired_tools_are_served_while_refreshing(self):
        self.assertEqual(self.apps_listed(), set(MANY))
        for app_id in MANY:
            cache.delete(tools.status._fresh_key("tools", app_id, GUEST))
        self.slow()
        started = time.monotonic()
        self.assertEqual(self.apps_listed(timeout=5.0), set(MANY))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_tools_dont_queue_behind_the_shared_pool(self):
        self.saturate_shared_pool()
        self.assertEqual(self.apps_listed(timeout=2.0), set(MANY))

    def test_first_listing_waits_then_logs_what_is_missing(self):
        self.slow("app01", "app02")
        with self.assertLogs("hub.tools", "WARNING") as logs:
            listed = self.apps_listed(timeout=0.5)
        self.assertEqual(listed, set(MANY) - {"app01", "app02"})
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r"\(guest\) took 0\.\ds without app01, app02")
        # They finish in the background, so the next listing has them.
        self.drain()
        self.assertEqual(self.apps_listed(), set(MANY))

    def test_warm_lists_ahead_of_the_first_conversation(self):
        self.assertEqual(tools.warm(), {GUEST: [], DASHBOARD: []})
        cache.clear()
        self.slow()
        for mode in (GUEST, DASHBOARD):
            self.assertEqual(self.apps_listed(mode, timeout=2.0), set(MANY))


def scenes_shortcuts():
    return [Shortcut(id="relax", title="Relax", icon="fa-solid fa-couch", modes=ALL_MODES)]


def scenes_run_shortcut(shortcut_id):
    STATE.setdefault("ran", []).append(shortcut_id)


OPTED_OUT = PLUGINS + (
    PluginSpec(id="vault", app="vault", tiles=(_tile("Vault"),), assistant=False,
               status="hub.tests.test_tools.lights_status", run_action="hub.tests.test_tools.lights_run_action",
               shortcuts="hub.tests.test_tools.lights_shortcuts",
               run_shortcut="hub.tests.test_tools.lights_run_shortcut"),
)


class AllowlistTests(GatewayTestCase):
    def test_only_allowlisted_apps_contribute(self):
        self.allow("heat")
        for mode in (OWNER, GUEST, DASHBOARD):
            names = self.names(mode)
            self.assertIn("heat__get_temp", names)
            self.assertFalse([n for n in names if n.startswith(("lights__", "alarm__", LONG_ID[:20]))], mode)

    def test_guessed_names_of_other_apps_are_refused(self):
        self.allow("heat")
        session = self.session()
        for name in ("lights__shortcut__movie", "lights__action____all_off", tool_name(LONG_ID, LONG_TOOL)):
            answer = tools.call(session, name, {})
            self.assertFalse(answer["ok"], name)
        self.assertNotIn("ran", STATE)
        self.assertNotIn("acted", STATE)
        self.assertNotIn("calls", STATE)

    def test_dropped_while_listed_is_refused_at_run_time(self):
        session = self.session()
        entry = tools.catalog(DASHBOARD)["lights__shortcut__movie"]
        self.allow("heat")
        self.assertFalse(tools._run(entry, {}, DASHBOARD, 5)["ok"])
        self.assertNotIn("ran", STATE)
        answer = tools.call(session, "heat__get_temp", {"room": "den"})
        self.assertTrue(answer["ok"], answer)

    def test_owner_list_is_filtered_too(self):
        self.allow("heat")
        self.client.force_login(User.objects.create_user("owner", password="pw", is_staff=True))
        names = {t["name"] for t in self.client.get("/hub/api/tools/").json()["tools"]}
        self.assertIn("heat__unlock", names)
        self.assertNotIn("lights__shortcut__lock_up", names)
        self.assertNotIn("alarm__action__house__arm", names)

    def test_hub_status_only_describes_allowlisted_apps(self):
        self.allow("heat")
        result = tools.call(self.session(), "hub__status", {})["result"]
        self.assertNotIn("Lights", [a["app"] for a in result["apps"]])
        self.allow("lights")
        result = tools.call(self.session(), "hub__status", {})["result"]
        self.assertIn("Lights", [a["app"] for a in result["apps"]])

    def test_empty_allowlist_leaves_only_hub_status(self):
        self.allow()
        self.assertEqual(self.names(OWNER), {"hub__status"})

    def test_default_is_scenes_and_fireplace(self):
        AppSetting.objects.filter(key="assistant_apps").delete()
        hub_store.clear()
        self.assertEqual(tools.chosen_apps(), {"scenes", "fireplace"})
        scenes = PluginSpec(id="scenes", app="scenes", shortcuts="hub.tests.test_tools.scenes_shortcuts",
                            run_shortcut="hub.tests.test_tools.scenes_run_shortcut")
        with override_settings(HUB_PLUGINS=PLUGINS + (scenes,)):
            self.assertEqual([s.id for s in tools.usable_specs()], ["scenes"])
            self.assertEqual(self.names(GUEST), {"hub__status", "scenes__shortcut__relax"})


@override_settings(HUB_PLUGINS=OPTED_OUT)
class OptOutTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.allow("lights", "heat", "vault")

    def test_opted_out_app_offers_nothing_even_when_allowlisted(self):
        for mode in (OWNER, GUEST, DASHBOARD):
            names = self.names(mode)
            self.assertIn("lights__shortcut__movie", names)
            self.assertFalse([n for n in names if n.startswith("vault")], mode)
        self.assertFalse(tools.call(self.session(), "vault__shortcut__movie", {})["ok"])
        self.assertNotIn("ran", STATE)

    def test_owner_list_and_hub_status_leave_it_out(self):
        self.client.force_login(User.objects.create_user("owner", password="pw", is_staff=True))
        names = {t["name"] for t in self.client.get("/hub/api/tools/").json()["tools"]}
        self.assertFalse([n for n in names if n.startswith("vault")])
        result = tools.call(self.session(), "hub__status", {})["result"]
        self.assertEqual(sorted(a["app"] for a in result["apps"]), ["Lights"])

    def test_container_app_manifest_opt_out(self):
        from broker import remote

        reply = manifest(id="cams", hooks=["shortcuts", "run_shortcut", "tools", "run_tool"], assistant=False)
        spec = remote.spec_from_manifest(make_install("cams", manifest=reply), reply)
        self.assertFalse(spec.assistant)
        self.assertIsNone(spec.tools)
        self.assertIsNone(spec.run_tool)
        self.assertFalse(tools.opted_in(spec))
        legacy = manifest(id="cams2")
        self.assertTrue(remote.spec_from_manifest(make_install("cams2", manifest=legacy), legacy).assistant)


class SessionTests(GatewayTestCase):
    def test_voice_session_follows_the_stay_and_is_never_owner(self):
        opened = tools.open_session("voice", "voice")
        self.assertEqual(opened["mode"], DASHBOARD)
        self.assertEqual(opened["expires_in"], 15 * 60)
        session = tools.load_session(opened["token"], "voice")
        self.start_stay()
        self.assertEqual(tools.session_mode(session["kind"]), GUEST)
        self.start_stay(owner=True)
        self.assertEqual(tools.session_mode(session["kind"]), GUEST)
        self.end_stay()
        self.assertEqual(tools.session_mode(session["kind"]), DASHBOARD)

    def test_mode_is_checked_at_call_time(self):
        session = self.session()
        self.start_stay()
        tools.call(session, "heat__set_temp", {"degrees": 20})
        self.end_stay()
        tools.call(session, "heat__set_temp", {"degrees": 21})
        self.assertEqual([c[2] for c in STATE["calls"]], [GUEST, DASHBOARD])

    def test_owner_and_unknown_kinds_cant_be_opened(self):
        for kind in ("owner", "admin", "", None):
            with self.assertRaises(ValueError):
                tools.open_session("voice", kind)

    def test_forged_expired_and_other_apps_tokens(self):
        token = tools.open_session("voice", "voice")["token"]
        with self.assertRaisesMessage(tools.BadSession, "another app"):
            tools.load_session(token, "cams")
        with self.assertRaises(tools.BadSession):
            tools.load_session(token[:-2] + ("AA" if not token.endswith("AA") else "BB"), "voice")
        forged = signing.dumps({"sid": "x", "kind": "voice", "app": "voice"}, salt="other.salt")
        with self.assertRaises(tools.BadSession):
            tools.load_session(forged, "voice")
        owner = signing.dumps({"sid": "x", "kind": "owner", "app": "voice"}, salt=tools.SALT)
        with self.assertRaises(tools.BadSession):
            tools.load_session(owner, "voice")
        for junk in ("", None, 42):
            with self.assertRaises(tools.BadSession):
                tools.load_session(junk, "voice")
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 16 * 60):
            with self.assertRaisesMessage(tools.BadSession, "expired"):
                tools.load_session(token, "voice")


class CallTests(GatewayTestCase):
    def test_app_tool(self):
        self.start_stay()
        answer = tools.call(self.session(), "heat__get_temp", {"room": "den"})
        self.assertEqual(answer, {"ok": True, "result": {"room": "den", "celsius": 21}})
        self.assertEqual(STATE["calls"], [("get_temp", {"room": "den"}, GUEST)])

    def test_bad_arguments_never_reach_the_app(self):
        session = self.session()
        for args in ({}, {"room": "attic"}, {"room": 3}, {"room": "den", "extra": 1}, "den", ["den"]):
            answer = tools.call(session, "heat__get_temp", args)
            self.assertFalse(answer["ok"], args)
            self.assertIn("invalid arguments", answer["error"])
        for args in ({"degrees": "20"}, {"degrees": 20.5}, {"degrees": 99}, {"degrees": True}):
            self.assertFalse(tools.call(session, "heat__set_temp", args)["ok"], args)
        self.assertNotIn("calls", STATE)
        self.assertTrue(tools.call(session, "heat__set_temp", {"degrees": 20.0})["ok"])
        self.assertEqual(STATE["calls"], [("set_temp", {"degrees": 20}, DASHBOARD)])

    def test_owner_only_tools_cant_be_called_by_guests(self):
        session = self.session()
        self.start_stay()
        for name in ("heat__unlock", "lights__shortcut__lock_up", "alarm__action__house__arm", "nope", None):
            answer = tools.call(session, name, {})
            self.assertFalse(answer["ok"], name)
        self.assertNotIn("calls", STATE)
        self.assertNotIn("ran", STATE)
        self.assertNotIn("acted", STATE)

    def test_shortcut_and_action(self):
        session = self.session()
        self.assertEqual(tools.call(session, "lights__shortcut__movie", {}),
                         {"ok": True, "result": "Enjoy the show"})
        self.assertEqual(STATE["ran"], ["movie"])
        self.assertEqual(tools.call(session, "lights__action____all_off", None), {"ok": True, "result": "All off"})
        self.assertEqual(STATE["acted"], [("lights", "", "all_off")])
        self.assertFalse(tools.call(session, "lights__shortcut__movie", {"x": 1})["ok"])

    def test_failures_are_answers_not_crashes(self):
        session = self.session()
        self.assertEqual(tools.call(session, "heat__fail", {}), {"ok": False, "error": "the heater is offline"})
        crash = tools.call(session, "heat__crash", {})
        self.assertEqual(crash, {"ok": False, "error": "the tool failed"})

    def test_slow_tool_times_out(self):
        STATE["release"] = threading.Event()
        self.addCleanup(STATE["release"].set)
        answer = tools.call(self.session(), "heat__slow", {}, timeout=0.2)
        self.assertEqual(answer, {"ok": False, "error": "the tool took too long"})

    def test_hub_status(self):
        self.start_stay()
        answer = tools.call(self.session(), "hub__status", {})
        self.assertTrue(answer["ok"])
        result = answer["result"]
        self.assertEqual(result["mode"], GUEST)
        lights = next(a for a in result["apps"] if a["app"] == "Lights")
        self.assertEqual(lights["status"], "3 lights on")
        self.assertEqual(lights["readings"], ["Inside: 21°C"])
        self.assertNotIn("Alarm", [a["app"] for a in result["apps"]])
        json.dumps(result)


class AuditEventTests(GatewayTestCase):
    def test_open_and_calls_are_published(self):
        rec = Recorder(self)
        opened = tools.open_session("voice", "voice", display="192.168.10.50")
        session = tools.load_session(opened["token"], "voice")
        tools.call(session, "heat__get_temp", {"room": "loft"})
        tools.call(session, "heat__fail", {})
        tools.call(session, "heat__unlock", {})
        events = rec.get()
        sid = opened["sid"]
        self.assertEqual([t for t, _ in events], [f"assistant/{sid}/open"] + [f"assistant/{sid}/call"] * 3)
        self.assertEqual({k: events[0][1][k] for k in ("sid", "mode", "kind", "display", "app")},
                         {"sid": sid, "mode": DASHBOARD, "kind": "voice", "display": "192.168.10.50", "app": "voice"})
        ok, failed, refused = (e[1] for e in events[1:])
        self.assertEqual({k: ok[k] for k in ("tool", "args", "ok", "error", "mode", "app", "display")},
                         {"tool": "heat__get_temp", "args": {"room": "loft"}, "ok": True, "error": None,
                          "mode": DASHBOARD, "app": "voice", "display": "192.168.10.50"})
        self.assertIn("loft", ok["result"])
        self.assertEqual((failed["ok"], failed["error"], failed["result"]), (False, "the heater is offline", None))
        self.assertEqual((refused["tool"], refused["ok"]), ("heat__unlock", False))
        self.assertIsInstance(ok["time"], float)


@override_settings(HUB_PLUGINS=PLUGINS)
class BrokerOpTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        bare = manifest(id="voice", tiles=[], hooks=[], settings=[], settings_pages=[], permissions=["tools"])
        self.voice = make_install("voice", manifest=bare)
        self.other = make_install("other", manifest={**bare, "id": "other"})
        self.ungranted = make_install("nogrant", manifest={**bare, "id": "nogrant"}, grants=[])

    def call(self, op, payload, install):
        return self.client.post(f"/_broker/rpc/{op}", json.dumps(payload), content_type="application/json",
                                SERVER_PORT="8099", HTTP_AUTHORIZATION=auth(install))

    def ok(self, op, payload, install=None):
        resp = self.call(op, payload, install or self.voice)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["result"]

    def refused(self, op, payload, install=None, code="forbidden", status=403):
        resp = self.call(op, payload, install or self.voice)
        self.assertEqual(resp.status_code, status, resp.content)
        self.assertEqual(resp.json()["error"]["code"], code)

    def test_open_list_call(self):
        opened = self.ok("tools.open", {"kind": "voice", "display": "10.0.0.5"})
        self.assertEqual(opened["mode"], DASHBOARD)
        listed = {t["name"] for t in self.ok("tools.list", {"token": opened["token"]})["tools"]}
        self.assertIn("heat__get_temp", listed)
        self.assertNotIn("heat__unlock", listed)
        answer = self.ok("tools.call", {"token": opened["token"], "name": "heat__get_temp", "args": {"room": "den"}})
        self.assertEqual(answer, {"ok": True, "result": {"room": "den", "celsius": 21}})
        refused = self.ok("tools.call", {"token": opened["token"], "name": "heat__unlock", "args": {}})
        self.assertFalse(refused["ok"])
        self.assertTrue(AuditEntry.objects.filter(app_id="voice", op="tools.call", allowed=True).exists())
        entry = AuditEntry.objects.filter(op="tools.call").first()
        self.assertNotIn(opened["token"], entry.detail)

    def test_the_app_cant_ask_for_owner(self):
        self.refused("tools.open", {"kind": "owner"}, code="invalid", status=400)
        self.refused("tools.open", {"kind": "voice", "mode": "owner", "display": 5}, code="invalid", status=400)
        opened = self.ok("tools.open", {"kind": "voice", "mode": "owner"})
        self.assertEqual(opened["mode"], DASHBOARD)

    def test_needs_the_tools_permission(self):
        token = tools.open_session("nogrant", "voice")["token"]
        self.refused("tools.open", {"kind": "voice"}, install=self.ungranted)
        self.refused("tools.list", {"token": token}, install=self.ungranted)
        self.refused("tools.call", {"token": token, "name": "hub__status"}, install=self.ungranted)
        # Requested but not granted by the admin.
        self.ungranted.requested_permissions = ["tools"]
        self.ungranted.save()
        self.refused("tools.open", {"kind": "voice"}, install=self.ungranted)

    def test_bad_tokens(self):
        token = self.ok("tools.open", {"kind": "voice"})["token"]
        for bad in ("", "nope", token + "x", None):
            self.refused("tools.list", {"token": bad}, code="bad_session", status=400)
            self.refused("tools.call", {"token": bad, "name": "hub__status"}, code="bad_session", status=400)
        # Another app (also granted tools) can't replay it.
        self.refused("tools.list", {"token": token}, install=self.other, code="bad_session", status=400)
        self.refused("tools.call", {"token": token, "name": "heat__set_temp", "args": {"degrees": 20}},
                     install=self.other, code="bad_session", status=400)
        self.assertNotIn("calls", STATE)

    def test_apps_cant_publish_assistant_events(self):
        install = make_install("pub", manifest={**manifest(id="pub"), "permissions": ["events.publish:#"]})
        resp = self.call("events.publish", {"topic": "assistant/x/call", "data": {"ok": True}}, install)
        self.assertEqual(resp.status_code, 403, resp.content)
        resp = self.call("events.publish", {"topic": "shared/x", "data": 1}, install)
        self.assertEqual(resp.status_code, 200, resp.content)


@override_settings(HUB_PLUGINS=PLUGINS)
class OwnerApiTests(GatewayTestCase):
    def test_staff_only(self):
        resp = self.client.get("/hub/api/tools/")
        self.assertNotEqual(resp.status_code, 200)
        user = User.objects.create_user("owner", password="pw", is_staff=True)
        self.client.force_login(user)
        resp = self.client.get("/hub/api/tools/")
        self.assertEqual(resp.status_code, 200)
        names = {t["name"] for t in resp.json()["tools"]}
        self.assertIn("heat__unlock", names)
        self.assertTrue(all("target" not in t for t in resp.json()["tools"]))


class GeneralSettingsTests(TestCase):
    def setUp(self):
        hub_store.clear()
        self.addCleanup(hub_store.clear)
        self.client.force_login(User.objects.create_user("owner", password="pw", is_staff=True,
                                                         is_superuser=True))

    def test_allowlist_picker(self):
        url = "/settings/location/"
        with override_settings(HUB_PLUGINS=OPTED_OUT):
            page = self.client.get(url).content.decode()
            self.assertIn('name="assistant_apps" value="lights"', page)
            self.assertRegex(page, r'value="vault"[^>]*disabled')
            # Untouched, the default stays the default.
            self.client.post(url, {"assistant_form": "1", "assistant_dashboard_control": "on"})
            self.assertFalse(AppSetting.objects.filter(key="assistant_apps").exists())
            self.client.post(url, {"assistant_form": "1", "assistant_dashboard_control": "on",
                                   "assistant_apps": ["lights", "vault", "nope"]})
            hub_store.clear()
            self.assertEqual(tools.chosen_apps(), {"lights"})
            self.client.post(url, {"assistant_form": "1", "assistant_dashboard_control": "on"})
            hub_store.clear()
            self.assertEqual(tools.chosen_apps(), set())

    def test_toggle_saves_the_setting(self):
        url = "/settings/location/"
        self.client.post(url, {"assistant_form": "1"})
        hub_store.clear()
        self.assertFalse(tools.dashboard_control())
        self.client.post(url, {"assistant_form": "1", "assistant_dashboard_control": "on"})
        hub_store.clear()
        self.assertTrue(tools.dashboard_control())
        # A post without the card (another form) leaves it alone.
        AppSetting.objects.filter(key="assistant_dashboard_control").update(value=False)
        hub_store.clear()
        self.client.post(url, {"units": "metric"})
        hub_store.clear()
        self.assertFalse(tools.dashboard_control())
