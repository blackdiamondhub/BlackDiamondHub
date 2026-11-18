"""Helpers shared by the capability tests (capabilities_*_test.py)."""
import json
import subprocess
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import quote

import pytest
import requests

from stack_test import ARTIFACTS, ENV, HUB_CONTAINER, poll

PRODUCER, CONSUMER, DENIED, FEED, STORE = "e2e_producer", "e2e_consumer", "e2e_denied", "e2e_feed", "e2e_store"
FAKE_HA = ENV.get("FAKE_HA_URL", "")
BROKER = ENV.get("BROKER_URL", "")


class PlatformGap(AssertionError):
    """A platform gap this suite found; only this satisfies the ``gap`` markers."""


def gap(reason):
    # strict: when the platform closes the gap the test XPASSes and fails the
    # job, so the marker has to go with the fix.
    return pytest.mark.xfail(strict=True, raises=PlatformGap, reason=reason)


def django(code):
    """Run ``code`` in the hub's Django shell; it must print one JSON line last."""
    proc = subprocess.run(["docker", "exec", "-i", HUB_CONTAINER, "/app/.venv/bin/python", "manage.py", "shell"],
                          input=code, capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, f"django shell failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def info(app):
    return django(f"""
import json
from broker.models import AppInstall
from broker.tokens import token_for
i = AppInstall.objects.get(app_id={app!r})
print(json.dumps({{"port": i.port, "token": token_for(i), "grants": sorted(i.grants), "version": i.version,
                  "granted": i.granted_permissions, "requested": i.requested_permissions, "enabled": i.enabled}}))
""")


@lru_cache(maxsize=None)
def port(app):
    return info(app)["port"]


@lru_cache(maxsize=None)
def token(app):
    return info(app)["token"]


def set_granted(app, permissions):
    """Store ``permissions`` as granted as-is (services.set_permissions would narrow them)."""
    django(f"""
import json
from broker.models import AppInstall
i = AppInstall.objects.get(app_id={app!r})
i.granted_permissions = {list(permissions)!r}
i.save(update_fields=["granted_permissions", "updated"])
print(json.dumps(sorted(i.grants)))
""")


def inspect(name, kind="container"):
    proc = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True)
    return json.loads(proc.stdout)[0] if proc.returncode == 0 else None


def running(app):
    state = inspect(f"bdh-app-{app}")
    return bool(state and state["State"]["Running"])


def app_url(app, path=""):
    return f"http://localhost:{port(app)}/{app}/{path}"


def app_get(app, path="", timeout=120):
    """GET one of the app's own pages through its hub port (waking it), past start-up 5xx."""
    def once():
        resp = requests.get(app_url(app, path), timeout=60)
        return None if resp.status_code in (502, 503, 504) else (resp,)

    return poll(f"{app}/{path}", once, timeout, interval=2)[0]


def rpc(app, op, payload=None):
    """Call broker ``op`` from inside ``app`` through the SDK client (the fixture's rpc view)."""
    resp = app_get(app, f"rpc/{op}?p={quote(json.dumps(payload or {}))}")
    assert resp.status_code == 200, f"{app} rpc {op}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def ok(app, op, payload=None):
    out = rpc(app, op, payload)
    assert out["ok"], f"{app} {op} refused: {out}"
    return out["result"]


def refused(app, op, payload=None):
    out = rpc(app, op, payload)
    assert not out["ok"] and out["code"] == "forbidden" and out["status"] == 403, f"{app} {op}: {out}"
    return out


def broker_rpc(app_token, op, payload=None):
    return requests.post(f"{BROKER}/rpc/{op}", json=payload or {}, timeout=30,
                         headers={"Authorization": f"Bearer {app_token}"})


def stop(app):
    django(f"from broker import supervisor; supervisor.stop({app!r}); print('{{}}')")
    poll(f"{app} stopped", lambda: not running(app), 60)


def ha_calls():
    return requests.get(f"{FAKE_HA}/_calls", timeout=10).json()


def ha_reset():
    requests.post(f"{FAKE_HA}/_reset", timeout=10).raise_for_status()


def denials(app):
    """Ids and ops of the broker's refusals recorded for ``app``."""
    return django(f"""
import json
from broker.models import AuditEntry
print(json.dumps(list(AuditEntry.objects.filter(app_id={app!r}, allowed=False).values_list("id", "op"))))
""")


def subscription(app):
    return django(f"""
import json
from broker.models import AppSubscription
s = AppSubscription.objects.filter(app_id={app!r}, kind="events").first()
print(json.dumps(None if s is None else s.payload))
""")


@contextmanager
def browser(width=1280, height=800):
    from selenium import webdriver

    opts = webdriver.ChromeOptions()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", f"--window-size={width},{height}"):
        opts.add_argument(arg)
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    # The hub's pages pull fonts/icons from CDNs the test doesn't need.
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*cdn.jsdelivr.net*", "*cdnjs.cloudflare.com*"]})
    try:
        yield driver
    finally:
        driver.quit()


def screenshot(driver, name):
    driver.save_screenshot(str(ARTIFACTS / f"{name}.png"))


def wait_js(driver, what, script, timeout=60):
    return poll(what, lambda: driver.execute_script(script), timeout, interval=0.5)
