"""Write the stack-e2e catalog.json, one entry per app version (newest first), as bdh-app-catalog's
builder.make_entry would from the SDK builder's bdh-app.json (with manifest):

    python3 catalog.py <out> <wheel url prefix> <base> \
        <bdh-app.json> <image> <wheel> <name> [<bdh-app.json> <image> <wheel> <name> ...]
    python3 catalog.py --name <app id>   # the display name the harness lists it under
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def entry(config, image, base, wheel, url_prefix, now):
    m = config["manifest"]
    return {
        "version": config["version"], "kind": "image", "hub_api": m["api_version"],
        "image": image, "base": base, "build": "e2e.1",
        "wheel_url": url_prefix + Path(wheel).name,
        "sha256": hashlib.sha256(Path(wheel).read_bytes()).hexdigest(),
        "permissions": sorted(set(m.get("permissions", []))),
        "network": m.get("network", "outbound"),
        "lifecycle": m.get("lifecycle", "on_demand"),
        "memory_mb": m.get("memory_mb") or 96,
        "companions": m.get("companions", []),
        "provides": sorted(set(m.get("provides", []))),
        "custom_base": bool(config.get("custom_base")),
        "changelog": "stack-e2e", "released_at": now, "built_at": now,
        "attestation": {"url": "", "signer_workflow": ""},
    }


def main(out, url_prefix, base, *apps):
    if not apps or len(apps) % 4:
        raise SystemExit(__doc__)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    catalog = {"schema": 1, "apps": {}}
    for i in range(0, len(apps), 4):
        app_json, image, wheel, name = apps[i:i + 4]
        config = json.loads(Path(app_json).read_text())
        app = catalog["apps"].setdefault(config["id"], {
            "name": name, "description": "stack-e2e", "repo": "blackdiamondhub/BlackDiamondHub",
            "versions": [],
        })
        app["versions"].append(entry(config, image, base, wheel, url_prefix, now))
    for app in catalog["apps"].values():
        app["versions"].sort(key=lambda v: tuple(int(p) for p in v["version"].split(".")), reverse=True)
    Path(out).write_text(json.dumps(catalog, indent=2) + "\n")


NAMES = {"e2e_probe": "Probe Viewer"}


if __name__ == "__main__":
    if sys.argv[1:2] == ["--name"]:
        app_id = sys.argv[2]
        print(NAMES.get(app_id) or app_id.replace("_", " ").title())
    else:
        main(*sys.argv[1:])
