"""End-to-end checks against the stack tests/e2e/run_stack.sh starts.

Run after run_stack.sh: ``pytest -p no:cacheprovider tests/e2e/stack_test.py``.
Tests run in file order and share the stack: the lifecycle test comes last so
nothing else talks to the hub while it waits out the RUNNING_TTL trust window.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin

import pytest
import requests

WORK = Path(__file__).parent / ".work"
ENV = dict(line.split("=", 1) for line in (WORK / "stack.env").read_text().split() if "=" in line)
HUB = ENV["HUB_URL"]
APP = ENV["APP_URL"]
APP_ID = ENV["APP_ID"]
APP_CONTAINER = ENV["APP_CONTAINER"]
HUB_CONTAINER = ENV["HUB_CONTAINER"]
ARTIFACTS = Path(ENV["E2E_ARTIFACTS"])
ARTIFACTS.mkdir(parents=True, exist_ok=True)

TILE_TITLE = "Probe Viewer"
CAM_NAME = "Probe Cam"
# The SDK mounts an app's URLconf at "<id>/".
PAGE = f"/{APP_ID}/"
ICON = f"/apps/{APP_ID}/_raw/static/{APP_ID}/icon.svg"
# broker/lifecycle.py RUNNING_TTL plus margin.
TRUST_WINDOW = 60 + 5
# Hub PR #352 fixes both known gaps below; once it is on main those tests run strict.
ROOT = Path(__file__).resolve().parents[2]
HAS_WAKE_FLAG = "_bdh_wake" in (ROOT / "broker" / "views.py").read_text()
HAS_RUNNING_PROBE = "async def _probe(" in (ROOT / "broker" / "lifecycle.py").read_text()


def poll(what, check, timeout, interval=1.0):
    """``check()``'s first truthy result, or fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            result = check()
            if result:
                return result
            last = result
        except (requests.RequestException, subprocess.CalledProcessError, ValueError) as exc:
            last = exc
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout}s waiting for {what} (last: {last!r})")
        time.sleep(interval)


def get(url, **kwargs):
    kwargs.setdefault("timeout", 60)
    return requests.get(urljoin(HUB, url), **kwargs)


def container():
    out = subprocess.run(["docker", "inspect", APP_CONTAINER], check=True, capture_output=True, text=True).stdout
    state = json.loads(out)[0]
    return {"id": state["Id"], "started": state["State"]["StartedAt"],
            "restarts": state["RestartCount"], "running": state["State"]["Running"]}


def manage(code):
    subprocess.run(["docker", "exec", HUB_CONTAINER, "/app/.venv/bin/python", "manage.py", "shell", "-c", code],
                   check=True, capture_output=True, text=True, timeout=120)


def assert_image(url):
    resp = get(url)
    assert resp.status_code == 200, f"{url}: {resp.status_code}"
    assert resp.headers.get("Content-Type", "").startswith("image/"), f"{url}: {resp.headers.get('Content-Type')}"
    assert resp.content, f"{url}: empty body"


def open_app():
    """GET the app's page through the hub's proxy port, riding out the "Starting…" page."""
    def ok():
        resp = requests.get(APP + PAGE, timeout=60)
        if resp.status_code == 503:
            return None
        assert resp.status_code == 200, resp.status_code
        return resp
    resp = poll("the app page", ok, timeout=120)
    assert 'id="probe-header"' in resp.text
    return resp


def settled():
    """The app container once it has kept the same identity for 10 s."""
    seen = poll("the app container", lambda: (c := container())["running"] and c, timeout=60)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(1)
        now = container()
        if now != seen:
            seen, deadline = now, time.monotonic() + 10
    return seen


@pytest.fixture(scope="module")
def saver():
    """The screensaver API once the hub has the fixture's background (providers are gathered async)."""
    def with_cam():
        body = get("/display/api/saver/").json()
        return body if body.get("cam") else None
    return poll("the saver to list the probe cam", with_cam, timeout=90, interval=2)


# --- (c) the hub shows the app --------------------------------------------------

def test_home_tile_uses_display_name():
    page = poll("the probe tile on the home page",
                lambda: (r := get("/")).ok and TILE_TITLE in r.text and r, timeout=90, interval=2)
    (ARTIFACTS / "home.html").write_text(page.text)
    assert ICON in page.text
    assert f'href="/apps/{APP_ID}{PAGE}"' in page.text
    assert not re.search(rf">\s*{APP_ID}\s*<", page.text), "tile labelled with the app id"


def test_shell_page_shows_the_app():
    shell = get(f"/apps/{APP_ID}{PAGE}")
    assert shell.status_code == 200
    assert TILE_TITLE in shell.text
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        pytest.skip("selenium not installed")
    opts = webdriver.ChromeOptions()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,800"):
        opts.add_argument(arg)
    driver = webdriver.Chrome(options=opts)
    try:
        driver.get(urljoin(HUB, f"/apps/{APP_ID}{PAGE}"))
        frame = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        driver.switch_to.frame(frame)
        # The iframe shows "Starting…" (reloading itself) until the app answers.
        header = WebDriverWait(driver, 120).until(EC.visibility_of_element_located((By.ID, "probe-header")))
        assert header.text == TILE_TITLE
        driver.switch_to.default_content()
        driver.save_screenshot(str(ARTIFACTS / "shell.png"))
    except Exception:
        driver.save_screenshot(str(ARTIFACTS / "shell-failed.png"))
        raise
    finally:
        driver.quit()


# --- (a) every image URL the hub hands out is an image --------------------------

def test_saver_and_tile_images(saver):
    assert saver["cam"]["name"] == CAM_NAME
    assert saver["cam"]["url"].startswith(f"/apps/{APP_ID}/_raw/"), saver["cam"]
    urls = [saver["cam"]["url"], ICON, *saver["stock"], *(p["url"] for p in saver["photos"])]
    for url in urls:
        assert_image(url)


@pytest.fixture(scope="module")
def stopped(saver):
    manage(f"from broker import supervisor; supervisor.stop({APP_ID!r})")
    poll("the app to stop", lambda: not container()["running"], timeout=60)
    return saver


@pytest.mark.xfail(not HAS_WAKE_FLAG, strict=False, reason=(
    "main's /_raw/ wakes a sleeping app only for /static/ files, so the saver's live cam 404s "
    "while the app sleeps; hub PR #352 adds the _bdh_wake flag the saver puts on cam URLs. "
    "Strict once #352 is on main."))
def test_sleeping_app_cam_image(stopped):
    assert_image(stopped["cam"]["url"])


def test_sleeping_app_tile_icon(stopped):
    # Served from the hub's per-version copy, or by waking the app.
    assert_image(ICON)


# --- (b) opening an app inside its trust window doesn't recreate it -------------

@pytest.mark.xfail(not HAS_RUNNING_PROBE, strict=False, reason=(
    "On main, the first open after RUNNING_TTL calls supervisor start, which recreates the "
    "container; hub PR #352 checks the supervisor's running state first. "
    "Strict once #352 is on main."))
def test_reopen_after_trust_window_keeps_container():
    open_app()
    opened = time.monotonic()
    before = settled()
    # Quiet until the hub's trust in "running" has lapsed; the app must stay up meanwhile.
    while time.monotonic() - opened < TRUST_WINDOW:
        assert container() == before, "app container changed while idle"
        time.sleep(5)
    open_app()
    after = settled()
    assert after["id"] == before["id"], "reopening recreated the app container"
    assert after["started"] == before["started"], "reopening restarted the app container"
    assert after["restarts"] == before["restarts"]


# --- the hub served no errors ----------------------------------------------------

ERROR_STATUS = re.compile(r'"[A-Z]+ [^"]*" (5\d\d) ')


def test_no_server_errors_in_hub_log():
    log = subprocess.run(["docker", "logs", HUB_CONTAINER], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True).stdout
    # 503 is the proxy's designed "Starting…" page (Retry-After) for a waking app.
    errors = [line for line in log.splitlines()
              if (m := ERROR_STATUS.search(line)) and m.group(1) != "503"]
    errors += [line for line in log.splitlines() if "Internal Server Error" in line or "Traceback" in line]
    assert not errors, "\n".join(errors[:50])
