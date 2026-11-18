import json
import os
from importlib import resources

from bdh_sdk.plugins import LIFECYCLE_ON_DEMAND, NETWORK_NONE, PluginSpec, Tile
from bdh_sdk.provides import BACKGROUNDS


def _variant():
    try:
        return json.loads(resources.files(__package__).joinpath("variant.json").read_text())
    except FileNotFoundError:
        return None


# variants.py builds other identities from this package with a variant.json.
VARIANT = _variant()

if VARIANT is None:
    # No background work, wake_every or status hooks: past the SDK runtime's one
    # events.subscribe at boot the app never calls the broker, which would refresh
    # the hub's trust window and hide the bug the lifecycle test looks for.
    spec = PluginSpec(
        id="e2e_probe",
        app="e2e_probe.apps.E2eProbeConfig",
        urls="e2e_probe.urls",
        provides={BACKGROUNDS: "e2e_probe.provides.backgrounds"},
        tiles=(Tile(title="Probe Viewer", url_name="e2e_probe_index", icon="e2e_probe/icon.svg",
                    section="mountain", subtitle="stack-e2e fixture"),),
        api_version=2,
        permissions=(),
        network=NETWORK_NONE,
        lifecycle=LIFECYCLE_ON_DEMAND,
        memory_mb=96,
    )
else:
    V = VARIANT
    broker = os.environ.get("BDH_BROKER_URL", "")
    # An unhealthy release: it dies on the hub, but passes the builder's smoke
    # test (whose broker is on 127.0.0.1).
    if V.get("crash") and broker and "127.0.0.1" not in broker:
        raise SystemExit(f"{V['id']} {V['version']}: crashing on purpose")
    tiles = ()
    if V.get("title"):
        tiles = (Tile(title=V["title"], url_name="e2e_probe_index", icon="e2e_probe/icon.svg",
                      section="mountain", subtitle=V["id"]),)
    spec = PluginSpec(
        id=V["id"],
        app="e2e_probe.apps.E2eProbeConfig",
        urls="e2e_probe.urls",
        tiles=tiles,
        status="e2e_probe.views.status" if V.get("status") else None,
        api_version=2,
        permissions=tuple(V.get("permissions", ())),
        network=V.get("network", NETWORK_NONE),
        lifecycle=V.get("lifecycle", LIFECYCLE_ON_DEMAND),
        memory_mb=96,
        companions=tuple(V.get("companions", ())),
        shares=tuple(V.get("shares", ())),
        reads=tuple(V.get("reads", ())),
    )
    if V.get("wake_every"):
        # Below the SDK's 300 s minimum, so the test doesn't wait minutes.
        object.__setattr__(spec, "wake_every", V["wake_every"])