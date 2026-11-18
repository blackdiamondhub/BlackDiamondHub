"""Checks for the app under test (run_stack.sh EXTRA_APP_DIR), run before
stack_test.py so its hub-log check covers this app too. Skipped without one."""
import re
import subprocess

import pytest
import requests

from stack_test import ARTIFACTS, ENV, WORK, assert_image, get, poll

APP_ID = ENV.get("EXTRA_APP_ID")
pytestmark = pytest.mark.skipif(not APP_ID, reason="no app under test (EXTRA_APP_DIR)")


@pytest.fixture(scope="module")
def tile():
    """The app's tile on the home page: (shell href, icon URLs)."""
    def find():
        page = get("/")
        href = re.search(rf'href="(/apps/{APP_ID}/[^"]*)"', page.text) if page.ok else None
        return href and (page, href.group(1))
    page, href = poll(f"the {APP_ID} tile on the home page", find, timeout=90, interval=2)
    (ARTIFACTS / f"home-{APP_ID}.html").write_text(page.text)
    icons = sorted(set(re.findall(rf'src="(/apps/{APP_ID}/_raw/[^"]+)"', page.text)))
    return href, icons


def test_installed_and_ready():
    line = next((ln for ln in (WORK / "install.txt").read_text().splitlines() if ln.startswith(f"{APP_ID} ")), "")
    assert ": ready on port" in line, line or f"no app_install output for {APP_ID}"


def test_tile_icons_are_images(tile):
    _, icons = tile
    for url in icons:
        assert_image(url)


def test_page_through_hub(tile):
    href, _ = tile
    shell = get(href)
    assert shell.status_code == 200, f"{href}: {shell.status_code}"
    port = re.search(r" on port (\d+) ", next(ln for ln in (WORK / "install.txt").read_text().splitlines()
                                              if ln.startswith(f"{APP_ID} "))).group(1)
    path = href.removeprefix(f"/apps/{APP_ID}")

    def served():
        resp = requests.get(f"http://localhost:{port}{path}", timeout=60)
        return None if resp.status_code == 503 else resp  # "Starting…" while it wakes
    resp = poll(f"{APP_ID}'s page", served, timeout=180, interval=2)
    assert resp.status_code == 200, f"{path}: {resp.status_code}"
    state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", f"bdh-app-{APP_ID}"],
                           capture_output=True, text=True)
    assert state.stdout.strip() == "true", f"bdh-app-{APP_ID}: {state.stdout or state.stderr}"
