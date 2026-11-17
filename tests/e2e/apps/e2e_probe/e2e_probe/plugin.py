from bdh_sdk.plugins import LIFECYCLE_ON_DEMAND, NETWORK_NONE, PluginSpec, Tile
from bdh_sdk.provides import BACKGROUNDS

# No background work and no broker calls: the e2e lifecycle test relies on the
# hub only learning the app is up from its own start/probe path.
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
