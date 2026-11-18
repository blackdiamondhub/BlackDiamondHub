# Run in the hub with `manage.py shell < install_caps.py`: install the
# capability tests' fixture identities with the grants the tests expect.
from broker import services
from broker.models import AppInstall

GRANTS = {
    "e2e_producer": None,  # everything it asks for
    "e2e_consumer": ["events.subscribe:e2e/allowed/#", "ha.read:light.kitchen"],
    "e2e_denied": [],
    "e2e_feed": None,
    "e2e_store": None,
}
VERSIONS = {"e2e_store": "1.0.0"}

for app_id, grants in GRANTS.items():
    services.install(app_id, VERSIONS.get(app_id), permissions=grants, wait=False)
for app_id in GRANTS:
    record = services.finish(app_id, timeout=180)
    print(app_id, record.version, "port", record.port, "granted", sorted(record.grants))
missing = [a for a in GRANTS if not AppInstall.objects.get(app_id=a).manifest]
if missing:
    raise SystemExit(f"no manifest for {missing}")
print("caps installed")
