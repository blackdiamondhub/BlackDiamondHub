"""Capability tests: what each broker capability lets an app do, and that what
it doesn't grant is refused with no effect downstream.

Runs on the "caps" stack (SUITE=caps in run_stack.sh): e2e_producer (broad
grants, outbound network), e2e_consumer (narrow grants, some requested
permissions not granted), e2e_denied (nothing granted), plus a fake Home
Assistant on the host. Every call an app makes goes through the real SDK
client inside its container (the fixture's rpc view).
"""
import base64
import io
import json
import re
import time
import uuid
from urllib.parse import unquote

import pytest
import requests
from PIL import Image

from caps import (
    CONSUMER, DENIED, FEED, PRODUCER, STORE, PlatformGap, app_get, app_url, broker_rpc, browser, denials,
    django, gap, ha_calls, ha_reset, info, inspect, ok, poll, refused, rpc, screenshot, set_granted,
    subscription, token, wait_js,
)
from stack_test import ENV, HUB, get, hub_log

pytestmark = pytest.mark.skipif(ENV.get("SUITE") != "caps", reason="needs the caps stack (SUITE=caps)")

START = time.time()
NONCE = uuid.uuid4().hex[:8]


def _calls(path):
    return [c for c in ha_calls() if c["path"] == path]


def _mentions(call, text):
    return text in unquote(json.dumps(call))


def _received(app):
    return [e["topic"] for e in app_get(app, "events").json()["events"]]


def _awake_and_subscribed(app, patterns):
    app_get(app, "events")
    poll(f"{app} subscribed to {patterns}", lambda: subscription(app) == patterns, 60)


# --- grants: refused, and nothing happens -------------------------------------

def test_denied_app_is_refused_everything_with_no_side_effect():
    ha_reset()
    _awake_and_subscribed(CONSUMER, ["e2e/allowed/#"])
    before = {i for i, _ in denials(DENIED)}
    tag = f"denied-{NONCE}"
    calls = {
        "alerts.push": {"app": DENIED, "key": tag, "title": tag, "embed_url": f"/{DENIED}/", "ttl": 5},
        "speakers.list": {},
        "speakers.announce": {"message": tag, "speakers": ["media_player.lounge"]},
        "speakers.tts": {"message": tag},
        "ha.get_tts": {"engine_id": "tts.e2e", "message": tag, "options": {}},
        "ha.upload_media": {"data": base64.b64encode(b"x").decode(), "folder": "e2e", "filename": f"{tag}.txt",
                            "content_type": "text/plain"},
        "ha.get_entity_state": {"entity_id": "light.kitchen"},
        "ha.call_service": {"domain": "lock", "service": "unlock", "entity_id": "lock.safe"},
        "ha.registry": {},
        "events.publish": {"topic": f"e2e/allowed/{tag}", "data": {}},
        "events.subscribe": {"patterns": ["e2e/#"]},
    }
    for op, payload in calls.items():
        refused(DENIED, op, payload)

    audited = [op for i, op in denials(DENIED) if i not in before]
    assert set(calls) <= set(audited), f"refusals not audited: {set(calls) - set(audited)}"
    # Nothing reached Home Assistant on its behalf...
    assert not [c for c in ha_calls() if _mentions(c, tag)]
    assert not _calls("/api/services/lock/unlock")
    assert not _calls("/api/services/media_player/play_media")
    assert not _calls("/api/states/light.kitchen")
    # ...no subscription was stored, and the event went nowhere (checked after a
    # delivered control event: delivery is in publish order).
    assert subscription(DENIED) in (None, [])
    control = f"e2e/allowed/control-{NONCE}"
    ok(PRODUCER, "events.publish", {"topic": control, "data": {}})
    poll("control event delivered", lambda: control in _received(CONSUMER), 30)
    assert f"e2e/allowed/{tag}" not in _received(CONSUMER)
    # (A refused alert reaching no screen is checked in the alert test, which
    # has one connected.)


def test_requested_but_not_granted_is_refused():
    ha_reset()
    assert "ha.call:light.turn_off" in info(CONSUMER)["requested"]
    refused(CONSUMER, "ha.call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})
    assert not _calls("/api/services/light/turn_off")


def test_granted_but_not_requested_is_refused():
    before = info(CONSUMER)
    assert "alerts" not in before["requested"]
    set_granted(CONSUMER, before["granted"] + ["alerts"])
    try:
        assert "alerts" not in info(CONSUMER)["grants"]
        refused(CONSUMER, "alerts.push", {"app": CONSUMER, "key": "k", "title": "nope", "embed_url": f"/{CONSUMER}/"})
    finally:
        set_granted(CONSUMER, before["granted"])


def test_storage_is_isolated_between_apps():
    ok(PRODUCER, "state.set", {"app": PRODUCER, "key": "secret", "value": NONCE})
    refused(CONSUMER, "state.get", {"app": PRODUCER, "key": "secret"})
    refused(CONSUMER, "state.set", {"app": PRODUCER, "key": "secret", "value": "overwritten"})
    refused(CONSUMER, "state.keys", {"app": PRODUCER})
    assert ok(PRODUCER, "state.get", {"app": PRODUCER, "key": "secret"}) == {"found": True, "value": NONCE}
    assert ok(CONSUMER, "state.get", {"app": CONSUMER, "key": "secret"})["found"] is False

    # Each app's /data volume is its own.
    assert app_get(PRODUCER, f"files/f{NONCE}?write={NONCE}").json() == {"found": True, "value": NONCE}
    assert app_get(CONSUMER, f"files/f{NONCE}").status_code == 404
    assert inspect(f"bdh-app-{PRODUCER}-data", "volume") and inspect(f"bdh-app-{CONSUMER}-data", "volume")


def test_bogus_token_is_rejected():
    resp = broker_rpc("not-a-token", "state.keys", {"app": PRODUCER})
    assert resp.status_code == 403, resp.text
    resp = broker_rpc(token(PRODUCER), "state.keys", {"app": PRODUCER})
    assert resp.status_code == 200 and resp.json()["ok"], resp.text


# --- shares -------------------------------------------------------------------

def _share_rows(provider):
    return django(f"""
import json
from broker.models import ShareRevision
print(json.dumps(ShareRevision.objects.filter(provider={provider!r}).count()))
""")


def test_shares_granted_read_only():
    png = app_get(PRODUCER, "bg.png").content
    sha = ok(PRODUCER, "shares.put_blob", {"mime": "image/png", "data_b64": base64.b64encode(png).decode()})["sha256"]
    rev = ok(PRODUCER, "shares.publish", {"name": "snap", "main": sha, "attachments": {}, "expected_revision": None})
    django("""
import json
from broker.models import ShareGrant
ShareGrant.objects.filter(consumer="e2e_consumer").delete()
print("{}")
""")
    refused(CONSUMER, "shares.open", {"provider": PRODUCER, "name": "snap"})
    django("""
from broker.models import ShareGrant
ShareGrant.objects.create(consumer="e2e_consumer", provider="e2e_producer", name="snap", granted_by="e2e")
print("{}")
""")
    opened = ok(CONSUMER, "shares.open", {"provider": PRODUCER, "name": "snap"})
    assert opened["revision"] == rev["revision"]
    data = base64.b64decode(opened["data_b64"])
    assert data == png
    assert Image.open(io.BytesIO(data)).size == (64, 36)
    # Only the grantee reads it; the denied app can't publish over it either.
    refused(DENIED, "shares.open", {"provider": PRODUCER, "name": "snap"})
    refused(DENIED, "shares.publish", {"name": "snap", "main": sha, "attachments": {}, "expected_revision": None})
    assert _share_rows(DENIED) == 0


# --- events -------------------------------------------------------------------

def test_events_reach_only_matching_subscribers():
    _awake_and_subscribed(CONSUMER, ["e2e/allowed/#"])
    blocked, allowed = f"e2e/blocked/{NONCE}", f"e2e/allowed/{NONCE}"
    ok(PRODUCER, "events.publish", {"topic": blocked, "data": {"n": 1}})
    refused(DENIED, "events.publish", {"topic": f"e2e/allowed/denied-{NONCE}", "data": {}})
    refused(PRODUCER, "events.publish", {"topic": f"_hub/{NONCE}", "data": {}})
    ok(PRODUCER, "events.publish", {"topic": allowed, "data": {"n": 2}})
    poll("allowed event delivered", lambda: allowed in _received(CONSUMER), 30)
    got = app_get(CONSUMER, "events").json()["events"]
    assert {"topic": allowed, "data": {"n": 2}} in got
    topics = [e["topic"] for e in got]
    assert blocked not in topics and f"e2e/allowed/denied-{NONCE}" not in topics
    # The publisher doesn't hear itself, and has no subscription.
    assert allowed not in _received(PRODUCER)

    # Asking for more than granted is refused and leaves the stored set alone.
    refused(CONSUMER, "events.subscribe", {"patterns": ["e2e/allowed/#", "e2e/#"]})
    assert subscription(CONSUMER) == ["e2e/allowed/#"]


def test_ha_read_revocation_is_immediate():
    before = info(CONSUMER)["granted"]
    assert ok(CONSUMER, "ha.get_entity_state", {"entity_id": "light.kitchen"})["state"] == "off"
    set_granted(CONSUMER, [p for p in before if not p.startswith("ha.read")])
    try:
        ha_reset()
        refused(CONSUMER, "ha.get_entity_state", {"entity_id": "light.kitchen"})
        assert not _calls("/api/states/light.kitchen")
    finally:
        set_granted(CONSUMER, before)


@gap("events.subscribe revocation only reaches the running hub at its next 60 s reconcile")
def test_event_grant_revocation_stops_delivery():
    before = info(CONSUMER)["granted"]
    try:
        # Usually the first round shows it; a few rounds keep a reconcile that
        # happens to land in between from passing the test by luck.
        for attempt in range(3):
            _awake_and_subscribed(CONSUMER, ["e2e/allowed/#"])
            set_granted(CONSUMER, [p for p in before if not p.startswith("events.")])
            topic = f"e2e/allowed/revoked-{NONCE}-{attempt}"
            ok(PRODUCER, "events.publish", {"topic": topic, "data": {}})
            # Delivery takes well under a second when it happens.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if topic in _received(CONSUMER):
                    raise PlatformGap(f"{topic} delivered after the grant was revoked")
                time.sleep(1)
            set_granted(CONSUMER, before)
            control = f"e2e/allowed/restored-{NONCE}-{attempt}"

            def delivered():
                ok(PRODUCER, "events.publish", {"topic": control, "data": {}})
                return control in _received(CONSUMER)

            poll("delivery after the grant is restored", delivered, 90, interval=5)
    finally:
        set_granted(CONSUMER, before)


# --- Home Assistant -----------------------------------------------------------

def test_home_assistant_reads_and_calls_within_grants():
    ha_reset()
    assert ok(PRODUCER, "ha.get_entity_state", {"entity_id": "light.kitchen"})["state"] == "off"
    assert _calls("/api/states/light.kitchen")
    refused(PRODUCER, "ha.get_entity_state", {"entity_id": "lock.safe"})
    assert not _calls("/api/states/lock.safe")

    ok(PRODUCER, "ha.call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
    turned = _calls("/api/services/light/turn_on")
    assert len(turned) == 1 and turned[0]["body"]["entity_id"] == "light.kitchen", turned
    refused(PRODUCER, "ha.call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.safe"})
    assert not _calls("/api/services/lock/unlock")

    assert ok(CONSUMER, "ha.get_entity_state", {"entity_id": "light.kitchen"})["entity_id"] == "light.kitchen"
    refused(CONSUMER, "ha.get_entity_state", {"entity_id": "light.porch"})
    assert not _calls("/api/states/light.porch")


def test_ha_registry_is_filtered_by_read_grants():
    reg = ok(PRODUCER, "ha.registry", {})
    assert sorted(e["entity_id"] for e in reg["entities"]) == ["light.kitchen", "light.porch"]
    assert [d["id"] for d in reg["devices"]] == ["dev-kitchen"]
    reg = ok(CONSUMER, "ha.registry", {})
    assert [e["entity_id"] for e in reg["entities"]] == ["light.kitchen"]
    assert [d["id"] for d in reg["devices"]] == ["dev-kitchen"]
    refused(DENIED, "ha.registry", {})


@gap("ha.call grants name a service, not entities: ha.call:light.turn_on can target lock.safe")
def test_ha_call_grant_is_entity_scoped():
    ha_reset()
    out = rpc(PRODUCER, "ha.call_service", {"domain": "light", "service": "turn_on", "entity_id": "lock.safe"})
    reached = [c for c in _calls("/api/services/light/turn_on") if c["body"].get("entity_id") == "lock.safe"]
    if out["ok"] or reached:
        raise PlatformGap(f"light.turn_on on lock.safe allowed (HA saw {len(reached)} call)")
    assert out["code"] == "forbidden"


# --- egress -------------------------------------------------------------------

def _env(app):
    return dict(e.split("=", 1) for e in inspect(f"bdh-app-{app}")["Config"]["Env"])


def test_network_policy_follows_manifest():
    for app in (PRODUCER, CONSUMER):
        app_get(app, "events")
    proxy = {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
    producer, consumer = _env(PRODUCER), _env(CONSUMER)
    assert proxy & set(producer), "outbound app has no egress proxy"
    assert not proxy & set(consumer), f"network:none app has an egress proxy: {proxy & set(consumer)}"
    for env in (producer, consumer):
        assert env.get("BDH_BROKER_URL")
    # Both sit on their own internal network: the only way out is the gateway.
    for app in (PRODUCER, CONSUMER):
        nets = inspect(f"bdh-app-{app}")["NetworkSettings"]["Networks"]
        assert list(nets) == [f"bdh-app-{app}"], nets
        assert inspect(f"bdh-app-{app}", "network")["Internal"] is True
    # A network:none app still reaches the broker.
    assert ok(CONSUMER, "state.keys", {"app": CONSUMER}) is not None


# --- stubbed hardware ---------------------------------------------------------

def test_speakers_announce_and_tts_through_fake_ha():
    ha_reset()
    assert ["media_player.lounge", "Lounge (Sonos)"] in ok(PRODUCER, "speakers.list", {})
    message = f"e2e hello {NONCE}"
    assert ok(PRODUCER, "speakers.announce", {"message": message, "speakers": ["media_player.lounge"]}) == []
    played = poll("announcement played", lambda: _calls("/api/services/media_player/play_media"), 30)
    assert played[0]["body"]["entity_id"] in ("media_player.lounge", ["media_player.lounge"])
    assert _mentions(played[0], message), played
    audio = ok(PRODUCER, "speakers.tts", {"message": f"tts {NONCE}"})
    assert base64.b64decode(audio["audio"]).startswith(b"ID3")


def test_media_upload_and_tts_through_fake_ha():
    ha_reset()
    ok(PRODUCER, "ha.upload_media", {"data": base64.b64encode(b"e2e-bytes").decode(), "folder": "e2e",
                                     "filename": f"{NONCE}.txt", "content_type": "text/plain"})
    uploads = _calls("/api/media_source/local_source/upload")
    assert len(uploads) == 1 and uploads[0]["body"]["filename"] == f"{NONCE}.txt", uploads
    out = ok(PRODUCER, "ha.get_tts", {"engine_id": "tts.e2e", "message": f"media {NONCE}", "options": {}})
    assert base64.b64decode(out["audio"]).startswith(b"ID3")
    assert [c for c in _calls("/api/tts_get_url") if _mentions(c, f"media {NONCE}")]


def test_companion_only_for_apps_that_ask():
    resp = app_get(PRODUCER, "companion")
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["url"]
    assert "BDH_GO2RTC_URL" not in _env(CONSUMER)
    assert app_get(CONSUMER, "companion").status_code == 404


# --- browser ------------------------------------------------------------------

SHELL = f"/apps/{PRODUCER}/{PRODUCER}/"


def _open_shell(driver, path=SHELL):
    driver.get(HUB + path)
    frame = wait_js(driver, "app frame", "return document.querySelector('iframe#bdh-app-frame')")
    driver.switch_to.frame(frame)
    poll("app content in the frame", lambda: driver.find_elements("css selector", "#probe-header"), 120)
    driver.switch_to.default_content()
    return frame


def test_home_tiles_only_for_apps_with_tiles():
    for app in (DENIED, FEED, STORE):
        assert info(app)["enabled"]
    html = get("/").text
    assert "Producer" in html and "Consumer" in html
    assert f"/apps/{PRODUCER}/" in html and f"/apps/{CONSUMER}/" in html
    for app in (DENIED, FEED, STORE):
        assert f"/apps/{app}/" not in html, f"{app} has no tile but is linked from home"


@pytest.mark.parametrize("width,height", [(1280, 800), (400, 800)])
def test_app_loads_in_shell_and_is_usable(width, height):
    with browser(width, height) as driver:
        frame = _open_shell(driver)
        screenshot(driver, f"shell-{width}x{height}")
        nav = driver.execute_script("return document.querySelector('nav.navbar').getBoundingClientRect().bottom")
        box = driver.execute_script("return arguments[0].getBoundingClientRect().toJSON()", frame)
        assert nav <= box["top"] + 0.5, f"header ({nav}) covers the app frame (top {box['top']})"
        driver.switch_to.frame(frame)
        btn = driver.execute_script(
            "return document.getElementById('probe-button').getBoundingClientRect().toJSON()")
        driver.switch_to.default_content()
        x, y = box["left"] + btn["left"] + btn["width"] / 2, box["top"] + btn["top"] + btn["height"] / 2
        hit = driver.execute_script("return document.elementFromPoint(arguments[0], arguments[1]) === arguments[2]",
                                    x, y, frame)
        assert hit, f"something covers the app's first control at {x},{y}"
        driver.switch_to.frame(frame)
        driver.find_element("id", "probe-button").click()
        poll("button clicked", lambda: driver.find_element("id", "probe-button").text == "clicked", 10, 0.2)


def test_shell_ignores_messages_from_other_origins():
    with browser() as driver:
        frame = _open_shell(driver)
        consumer_origin = app_url(CONSUMER).split(f"/{CONSUMER}/")[0]
        assert frame.get_attribute("data-app-origin") != consumer_origin
        driver.execute_script("""
window.__titles = [];
new MutationObserver(() => window.__titles.push(document.getElementById('bdh-app-title').textContent))
  .observe(document.getElementById('bdh-app-title'), {childList: true, characterData: true, subtree: true});
const f = document.createElement('iframe');
f.id = 'other-app'; f.src = arguments[0]; f.onload = () => { window.__other = true; };
document.body.appendChild(f);
window.postMessage({bdh: 1, type: 'title', title: 'HACKED-top'}, '*');
""", app_url(CONSUMER))
        wait_js(driver, "other app frame", "return window.__other === true")
        driver.switch_to.frame(driver.find_element("id", "other-app"))
        poll("other app content", lambda: driver.find_elements("css selector", "#probe-header"), 60)
        driver.execute_script("parent.postMessage({bdh: 1, type: 'title', title: 'HACKED'}, '*')")
        driver.switch_to.default_content()
        driver.switch_to.frame(frame)
        driver.execute_script("parent.postMessage({bdh: 1, type: 'title', title: 'Real title'}, '*')")
        driver.switch_to.default_content()
        wait_js(driver, "real title", "return document.getElementById('bdh-app-title').textContent === 'Real title'")
        titles = driver.execute_script("return window.__titles")
        assert not [t for t in titles if "HACKED" in t], titles


def test_app_refuses_to_be_framed_by_another_origin():
    resp = requests.get(app_url(PRODUCER), timeout=60)
    csp = resp.headers.get("Content-Security-Policy", "")
    assert f"frame-ancestors {HUB}" in csp, csp
    script = """
const f = document.createElement('iframe');
f.id = 'framed'; f.src = arguments[0]; f.onload = () => { window.__framed = true; };
document.body.appendChild(f);
"""
    with browser() as driver:
        # Control: the hub's own origin may frame it.
        driver.get(HUB + "/")
        driver.execute_script(script, app_url(PRODUCER))
        wait_js(driver, "frame loaded on the hub", "return window.__framed === true")
        driver.switch_to.frame(driver.find_element("id", "framed"))
        poll("app content framed by the hub", lambda: driver.find_elements("css selector", "#probe-header"), 60)
        driver.switch_to.default_content()

        driver.get(app_url(CONSUMER))
        driver.execute_script(script, app_url(PRODUCER))
        wait_js(driver, "frame loaded on another app", "return window.__framed === true")
        screenshot(driver, "framed-by-other-origin")
        driver.switch_to.frame(driver.find_element("id", "framed"))
        assert not driver.find_elements("css selector", "#probe-header")


# --- alerts -------------------------------------------------------------------

def test_alert_wakes_saver_and_saver_resumes():
    with browser(1280, 800) as driver:
        driver.get(HUB + "/?saver=1")
        wait_js(driver, "screensaver on", "return !!document.querySelector('div.bdh-saver.on')", 30)
        driver.execute_script("""
window.__resumed = 0;
const resume = window.BDHSaver.resume;
window.BDHSaver.resume = function () { window.__resumed++; return resume.apply(this, arguments); };
""")
        key = f"alert-{NONCE}"

        def shown():
            # Alerts only go to screens connected at the time: push until this one is.
            ok(PRODUCER, "alerts.push", {"app": PRODUCER, "key": key, "title": f"E2E {NONCE}", "body": "body",
                                         "ttl": 60, "embed_url": f"/{PRODUCER}/"})
            return driver.execute_script("return !!document.querySelector('div.hub-alert.on')")

        poll("alert shown", shown, 60, interval=2)
        screenshot(driver, "alert")
        assert driver.find_element("css selector", ".hub-alert-title").text == f"E2E {NONCE}"
        assert not driver.execute_script("return !!document.querySelector('div.bdh-saver.on')"), "saver still up"
        # With a screen known to be connected, a refused alert must not show on it.
        refused(DENIED, "alerts.push", {"app": DENIED, "key": key, "title": f"DENIED {NONCE}", "ttl": 60,
                                        "embed_url": f"/{DENIED}/"})
        ok(PRODUCER, "alerts.push", {"app": PRODUCER, "key": f"{key}-2", "title": f"Second {NONCE}", "ttl": 60,
                                     "embed_url": f"/{PRODUCER}/"})
        wait_js(driver, "second alert", f"return document.body.innerText.includes('Second {NONCE}')", 30)
        assert f"DENIED {NONCE}" not in driver.find_element("tag name", "body").text
        driver.find_element("css selector", ".hub-alert-close").click()
        wait_js(driver, "alert closed and saver resumed",
                "return !document.querySelector('div.hub-alert.on') && window.__resumed > 0", 10)


# --- last: the hub logged no server errors for any of the above ---------------

def test_zz_no_server_errors():
    log = hub_log(START)
    errors = [m.group(0) for m in re.finditer(r'"[A-Z]+ [^"]*" (5\d\d) ', log) if m.group(1) != "503"]
    assert not errors, errors[:20]
    assert "Traceback" not in log and "Internal Server Error" not in log, log[-4000:]
