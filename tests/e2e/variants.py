"""Copy the e2e_probe fixture into one source tree per capability-test identity.

Usage: variants.py <outdir>. Prints "<id> <version> <dir>" per variant. Each
copy gets its own wheel name, version and entry-point id, and a variant.json
that plugin.py turns into that identity's manifest.
"""
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "apps" / "e2e_probe"

PRODUCER_PERMISSIONS = [
    "events.publish:e2e/#", "alerts", "speakers", "media", "ha.read:light.*", "ha.call:light.turn_on",
]
CONSUMER_PERMISSIONS = ["events.subscribe:e2e/allowed/#", "ha.read:light.kitchen", "ha.call:light.turn_off"]
DENIED_PERMISSIONS = [
    "alerts", "speakers", "media", "ha.read:*", "ha.call:light.turn_on", "ha.call:lock.unlock",
    "events.publish:e2e/#", "events.subscribe:e2e/#",
]

STORE_PERMISSIONS = ["events.subscribe:e2e_store/#"]

VARIANTS = [
    {"id": "e2e_producer", "version": "1.0.0", "title": "Producer", "status": True, "refresh": 5,
     "permissions": PRODUCER_PERMISSIONS, "network": "outbound", "companions": ["go2rtc"],
     "shares": [{"name": "snap", "type": "image/png", "title": "Snap"}]},
    {"id": "e2e_consumer", "version": "1.0.0", "title": "Consumer", "permissions": CONSUMER_PERMISSIONS,
     "reads": ["image/png"], "subscribe": ["e2e/allowed/#"]},
    {"id": "e2e_denied", "version": "1.0.0", "permissions": DENIED_PERMISSIONS},
    {"id": "e2e_feed", "version": "1.0.0", "status": True, "wake_every": 20},
    {"id": "e2e_store", "version": "1.0.0", "subscribe": ["e2e_store/#"], "permissions": STORE_PERMISSIONS},
    {"id": "e2e_store", "version": "2.0.0", "subscribe": ["e2e_store/#"], "permissions": STORE_PERMISSIONS},
    {"id": "e2e_store", "version": "3.0.0", "subscribe": ["e2e_store/#"], "permissions": STORE_PERMISSIONS, "crash": True},
]


def build(outdir, variant):
    app_id, version = variant["id"], variant["version"]
    dest = outdir / f"{app_id}-{version}"
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"))
    pyproject = dest / "pyproject.toml"
    text = pyproject.read_text()
    text = re.sub(r'(?m)^name = ".*"$', f'name = "bdh-app-{app_id.replace("_", "-")}"', text)
    text = re.sub(r'(?m)^version = ".*"$', f'version = "{version}"', text)
    text = re.sub(r'(?m)^e2e_probe = "e2e_probe.plugin:spec"$', f'{app_id} = "e2e_probe.plugin:spec"', text)
    pyproject.write_text(text)
    (dest / "e2e_probe" / "variant.json").write_text(json.dumps(variant, indent=1))
    return dest


def main():
    outdir = Path(sys.argv[1]).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    for variant in VARIANTS:
        print(variant["id"], variant["version"], build(outdir, variant))


if __name__ == "__main__":
    main()
