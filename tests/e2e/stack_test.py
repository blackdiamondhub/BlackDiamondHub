"""End-to-end checks against the stack tests/e2e/run_stack.sh starts.

Run after run_stack.sh: ``pytest -p no:cacheprovider tests/e2e/stack_test.py``.
Tests run in file order and share the stack: the lifecycle test comes last so
nothing else talks to the hub while it waits out the RUNNING_TTL trust window.
"""
import io
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import pytest
import requests
from PIL import Image

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
# What e2e_probe's bg view draws.
CAM_SIZE = (64, 36)
CAM_RGB = (30, 90, 160)
# Hub requests that refresh (or bypass) the trust window for the app.
APP_TRAFFIC = re.compile(rf"/_broker/|/apps/{APP_ID}[/?]|supervisor/apps/{APP_ID}/")


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
    """The app container's identity and state, or None when it doesn't exist."""
    proc = subprocess.run(["docker", "inspect", APP_CONTAINER], capture_output=True, text=True)
    if proc.returncode:
        if "No such object" in proc.stderr:
            return None
        proc.check_returncode()
    state = json.loads(proc.stdout)[0]
    return {"id": state["Id"], "started": state["State"]["StartedAt"],
            "restarts": state["RestartCount"], "running": state["State"]["Running"]}


def manage(code):
    subprocess.run(["docker", "exec", HUB_CONTAINER, "/app/.venv/bin/python", "manage.py", "shell", "-c", code],
                   check=True, capture_output=True, text=True, timeout=120)


def hub_log(since=None):
    cmd = ["docker", "logs", *(["--since", f"{since:.3f}"] if since else []), HUB_CONTAINER]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout


def assert_image(url, resp=None):
    """``url`` serves a well-formed image of the type it claims; returns it decoded (None for SVG)."""
    resp = resp or get(url)
    assert resp.status_code == 200, f"{url}: {resp.status_code}"
    kind = resp.headers.get("Content-Type", "").split(";")[0].strip()
    assert kind.startswith("image/"), f"{url}: Content-Type {kind!r}"
    if kind == "image/svg+xml":
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            pytest.fail(f"{url}: not SVG ({exc}): {resp.content[:80]!r}")
        assert root.tag == "{http://www.w3.org/2000/svg}svg", f"{url}: root element {root.tag!r}"
        return None
    try:
        Image.open(io.BytesIO(resp.content)).verify()
        img = Image.open(io.BytesIO(resp.content))
        img.load()
    except Exception as exc:
        pytest.fail(f"{url}: {kind} body isn't a decodable image ({exc}): {resp.content[:80]!r}")
    assert Image.MIME.get(img.format) == kind, f"{url}: {kind} but the body is {img.format}"
    assert img.width > 0 and img.height > 0, f"{url}: {img.size}"
    return img


def assert_probe_cam(url, resp=None):
    img = assert_image(url, resp)
    assert img is not None and img.size == CAM_SIZE, f"{url}: not the probe's cam image"
    assert img.convert("RGB").getpixel((0, 0)) == CAM_RGB, f"{url}: not the probe's cam image"


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


def settled(quiet=10, timeout=90):
    """The app container once it has kept the same identity, running, for ``quiet`` s."""
    give_up = time.monotonic() + timeout
    seen, since = None, time.monotonic()
    while True:
        now = container()
        if now != seen or not (now and now["running"]):
            seen, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return seen
        if time.monotonic() >= give_up:
            pytest.fail(f"app container didn't settle within {timeout}s (last: {seen})")
        time.sleep(1)


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
    driver.set_page_load_timeout(60)
    # base.html loads Bootstrap from a CDN with a blocking <script> in <head>; a
    # stalled CDN fetch once kept the parser from ever reaching the iframe. The
    # test is about the hub and the app, so keep third-party hosts out of it.
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*cdn.jsdelivr.net*", "*cdnjs.cloudflare.com*"]})
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
    assert_probe_cam(urls[0])
    for url in urls[1:]:
        assert_image(url)


def is_asleep():
    c = container()
    return c is None or not c["running"]


@pytest.fixture
def asleep():
    """Stop the app through the supervisor right before each test that needs it asleep."""
    manage(f"from broker import supervisor; supervisor.stop({APP_ID!r})")
    poll("the app to stop", is_asleep, timeout=60)


def test_sleeping_app_cam_image(saver, asleep):
    assert is_asleep()
    url = saver["cam"]["url"]
    resp = get(url)
    assert resp.status_code != 404, f"{url}: 404 while the app sleeps"
    assert_probe_cam(url, resp)


def test_sleeping_app_tile_icon(asleep):
    assert is_asleep()
    # Served from the hub's per-version copy, or by waking the app.
    assert_image(ICON)


# --- (b) opening an app inside its trust window doesn't recreate it -------------

def test_reopen_after_trust_window_keeps_container():
    open_app()
    # settled() outlasts the app's one boot-time broker call (events.subscribe), the
    # last thing that refreshes the hub's trust window before the quiet spell.
    before = settled()
    quiet_from = time.time()
    # Watch through docker only: any request to the hub about the app would wake it
    # or refresh the trust window and hide the bug.
    while time.time() - quiet_from < TRUST_WINDOW:
        assert container() == before, "app container changed while idle"
        time.sleep(5)
    traffic = [line for line in hub_log(since=quiet_from).splitlines() if APP_TRAFFIC.search(line)]
    assert not traffic, "app traffic during the quiet spell refreshed the trust window:\n" + "\n".join(traffic)
    open_app()
    after = settled()
    assert after["id"] == before["id"], (
        f"reopening recreated the app container ({before['id'][:12]} -> {after['id'][:12]})")
    assert after["started"] == before["started"], "reopening restarted the app container"
    assert after["restarts"] == before["restarts"]


# --- the hub served no errors ----------------------------------------------------

ERROR_STATUS = re.compile(r'"[A-Z]+ [^"]*" (5\d\d) ')


def test_no_server_errors_in_hub_log():
    log = hub_log()
    # 503 is the proxy's designed "Starting…" page (Retry-After) for a waking app.
    errors = [line for line in log.splitlines()
              if (m := ERROR_STATUS.search(line)) and m.group(1) != "503"]
    errors += [line for line in log.splitlines() if "Internal Server Error" in line or "Traceback" in line]
    assert not errors, "\n".join(errors[:50])
