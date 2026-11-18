"""Capability tests that wait on the app lifecycle: sleeping apps' status,
wake_every, and an install's upgrade/rollback/uninstall sequence.

Runs on the "caps-life" stack (SUITE=caps-life in run_stack.sh), next to the
"caps" leg so the waits overlap. Container state is read with docker inspect,
never with requests that would wake the app.
"""
import time

import pytest
import requests

from caps import (
    FEED, PRODUCER, STORE, PlatformGap, app_get, broker_rpc, django, gap, info, inspect, ok, poll, port,
    running, stop, subscription, token,
)
from stack_test import ENV, get

pytestmark = pytest.mark.skipif(ENV.get("SUITE") != "caps-life", reason="needs the caps stack (SUITE=caps-life)")


def _status(app):
    return get("/hub/api/status/").json()["status"].get(app)


def _started(app):
    state = inspect(f"bdh-app-{app}")
    return state and state["State"]["StartedAt"]


# --- status while asleep ------------------------------------------------------

def test_sleeping_app_tile_is_served_from_cache():
    app_get(PRODUCER, "")
    shown = poll("producer status", lambda: (_status(PRODUCER) or {}).get("text", "").startswith("probe status #")
                 and _status(PRODUCER), 60)
    stop(PRODUCER)
    started = _started(PRODUCER)
    # Past the 5 s status refresh, so each request asks the (sleeping) app again.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        now = _status(PRODUCER)
        assert now and now["text"] == shown["text"], now
        assert not running(PRODUCER), "asking a sleeping app's status woke it"
        time.sleep(3)
    assert _started(PRODUCER) == started


@gap("there is no status.push op: a sleeping app can't update its tile without being woken")
def test_status_push_updates_tile_without_waking():
    if running(PRODUCER):
        stop(PRODUCER)
    resp = broker_rpc(token(PRODUCER), "status.push", {"app": PRODUCER, "text": "pushed while asleep"})
    if resp.status_code != 200 or not resp.json().get("ok"):
        raise PlatformGap(f"status.push: {resp.status_code} {resp.text[:200]}")
    poll("pushed status", lambda: (_status(PRODUCER) or {}).get("text") == "pushed while asleep", 30)
    assert not running(PRODUCER)


# --- wake_every ---------------------------------------------------------------

def test_background_app_goes_back_to_sleep():
    app_get(FEED, "status-log")
    assert running(FEED)
    # The supervisor's idle timeout is 75 s on this stack; nothing here touches the app meanwhile.
    poll("feed asleep", lambda: not running(FEED), 180, interval=3)


@gap("the hub's waker polls tile-less apps every 600 s, so a shorter wake_every (20 s here) is not honoured")
def test_wake_every_wakes_sleeping_app():
    if running(FEED):
        stop(FEED)
    started = _started(FEED)
    # Two wake_every periods plus slack.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if running(FEED) or _started(FEED) != started:
            break
        time.sleep(2)
    else:
        raise PlatformGap("feed was not woken within 2x its wake_every")
    poll("feed asleep again", lambda: not running(FEED), 180, interval=3)


# --- upgrade / rollback / uninstall -------------------------------------------

def _upgrade(version, timeout=180, wait=True):
    return django(f"""
import json
from broker import services
from broker.models import AppInstall
try:
    r = services.upgrade(AppInstall.objects.get(app_id={STORE!r}), services._entry({STORE!r}, {version!r}),
                         wait={wait!r}, timeout={timeout})
    print(json.dumps({{"ok": True, "version": r.version}}))
except Exception as exc:
    print(json.dumps({{"ok": False, "error": repr(exc)}}))
""")


def _supervisor(app):
    return django(f"""
import json
from broker import supervisor
try:
    a = supervisor.get_app({app!r})
    keys = ("version", "previous_version", "rollback_available", "upgrade_stage", "running", "status", "error")
    print(json.dumps({{k: a.get(k) for k in keys}}))
except supervisor.NotFound:
    print(json.dumps(None))
""")


def _data_intact():
    assert app_get(STORE, "files/data").json() == {"found": True, "value": "v1"}
    assert ok(STORE, "state.get", {"app": STORE, "key": "k"}) == {"found": True, "value": "v1"}
    assert ok(STORE, "settings.get", {"app": STORE, "key": "color"}) == {"found": True, "value": "blue"}


def test_store_upgrade_keeps_data():
    assert info(STORE)["version"] == "1.0.0"
    assert app_get(STORE, "files/data?write=v1").json() == {"found": True, "value": "v1"}
    ok(STORE, "state.set", {"app": STORE, "key": "k", "value": "v1"})
    django(f"""
from hub_settings.models import AppSetting
AppSetting.objects.update_or_create(app_id={STORE!r}, key="color", defaults={{"value": "blue"}})
print("{{}}")
""")
    _data_intact()
    poll("store subscribed", lambda: subscription(STORE) == ["e2e_store/#"], 60)

    assert _upgrade("2.0.0") == {"ok": True, "version": "2.0.0"}
    assert _supervisor(STORE)["version"] == "2.0.0"
    _data_intact()
    assert subscription(STORE) == ["e2e_store/#"]


@gap("no health gate on upgrade: an update that can't start stays installed with no automatic rollback")
def test_unhealthy_upgrade_rolls_back():
    # Not waiting: the hub's own wait keeps restarting the crashing app to
    # fetch its manifest well past its timeout.
    result = _upgrade("3.0.0", wait=False)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        sup = _supervisor(STORE) or {}
        if sup.get("version") == "2.0.0" and sup.get("status") == "installed":
            assert app_get(STORE, "files/data").json() == {"found": True, "value": "v1"}
            return
        time.sleep(5)
    raise PlatformGap(f"3.0.0 left in place: upgrade={result} supervisor={_supervisor(STORE)}")


@gap("a supervisor rollback isn't reflected in the hub: AppInstall.version keeps the rolled-back-from version")
def test_rollback_recovers_data_and_hub_agrees():
    if _supervisor(STORE)["version"] != "2.0.0":
        django(f"from broker import supervisor; supervisor.request('POST', '/apps/{STORE}/rollback'); print('{{}}')")
    poll("supervisor back on 2.0.0", lambda: _supervisor(STORE)["version"] == "2.0.0", 120, interval=3)
    _data_intact()
    hub = info(STORE)["version"]
    if hub != "2.0.0":
        raise PlatformGap(f"supervisor runs 2.0.0, hub says {hub}")


def test_uninstall_removes_token_routes_and_subscriptions():
    tok, app_port = token(STORE), port(STORE)
    django(f"from broker import services; services.uninstall({STORE!r}, purge=True); print('{{}}')")

    assert broker_rpc(tok, "state.keys", {"app": STORE}).status_code == 403
    assert django(f"""
import json
from broker.models import AppInstall, AppSubscription
print(json.dumps([AppInstall.objects.filter(app_id={STORE!r}).exists(),
                  AppSubscription.objects.filter(app_id={STORE!r}).exists()]))
""") == [False, False]
    assert _supervisor(STORE) is None
    for name, kind in ((f"bdh-app-{STORE}", "container"), (f"bdh-app-{STORE}-data", "volume"),
                       (f"bdh-app-{STORE}", "network")):
        poll(f"{kind} {name} removed", lambda: inspect(name, kind) is None, 60)
    # Uninstalling from another process (manage.py) reaches the server's spec
    # cache at its next one-minute re-read.
    poll(f"/apps/{STORE}/ gone", lambda: get(f"/apps/{STORE}/").status_code == 404, 90, interval=3)
    try:
        resp = requests.get(f"http://localhost:{app_port}/{STORE}/files/data", timeout=10)
        assert resp.status_code != 200, f"old app port still serves the app: {resp.text[:200]}"
    except requests.ConnectionError:
        pass


@gap("uninstall (even with purge) leaves the app's hub-side state and settings behind")
def test_uninstall_purges_hub_side_data():
    left = django(f"""
import json
from hub_settings.models import AppSetting, AppState
print(json.dumps({{"state": AppState.objects.filter(app_id={STORE!r}).count(),
                  "settings": AppSetting.objects.filter(app_id={STORE!r}).count()}}))
""")
    if any(left.values()):
        raise PlatformGap(f"left behind after uninstall: {left}")
