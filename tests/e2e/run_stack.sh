#!/usr/bin/env bash
# Boot the hub + bdh-supervisor stack the way homehub runs it, with the
# e2e_probe fixture app installed as a container app, for stack_test.py:
#
#   BDH_SDK=.sdk bash tests/e2e/run_stack.sh
#
# - a local registry (localhost:5000) holding the published bdh-app-runtime
#   base and the fixture image built on it by the SDK's builder, both pinned
#   by digest;
# - a catalog.json listing the fixture, served over http on the host;
# - bdh-supervisor (published image) and the hub image built from this
#   checkout (HUB_SITE=e2e), both on the host network sharing the API socket;
# - the fixture installed with `manage.py app_install`.
#
# BDH_SDK is a checkout of blackdiamondlodge/bdh-sdk (CI uses the v0 tag).
# Writes tests/e2e/.work/stack.env (HUB_URL, APP_URL, ...) for the tests.
# Containers are named bdh-e2e-*; tests/e2e/collect_logs.sh gathers their logs.
set -euo pipefail
cd "$(dirname "$0")/../.."
sdk=$(cd "${BDH_SDK:?set BDH_SDK to a bdh-sdk checkout}" && pwd)
work=$PWD/tests/e2e/.work
registry=localhost:5000
prefix=$registry/blackdiamondlodge/
runtime=${RUNTIME_IMAGE:-ghcr.io/blackdiamondlodge/bdh-app-runtime:hub-api-2}
supervisor_image=${SUPERVISOR_IMAGE:-ghcr.io/blackdiamondlodge/bdh-supervisor:latest}
hub_image=${HUB_IMAGE:-bdh-e2e-hub}
catalog_port=8765
hub_port=8000
token=$(openssl rand -hex 32)

wait_for() {  # <seconds> <what> <command...>
    local deadline=$((SECONDS + $1)) what=$2
    shift 2
    until "$@" >/dev/null 2>&1; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "timed out after $1s waiting for $what" >&2
            return 1
        fi
        sleep 1
    done
    echo "ready: $what"
}

push() {  # <local image> <repo:tag> -> repo@sha256:...
    docker tag "$1" "$2" && docker push -q "$2" >/dev/null
    docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$2" | grep "^${2%:*}@" | head -n1
}

rm -rf "$work" && mkdir -p "$work/catalog"

echo "::group::registry and runtime base"
docker run -d --name bdh-e2e-registry -p 127.0.0.1:5000:5000 registry:2 >/dev/null
wait_for 60 registry curl -fs "http://$registry/v2/"
docker pull -q "$runtime" >/dev/null
base=$(push "$runtime" "${prefix}bdh-app-runtime:e2e")
echo "base: $base"
echo "::endgroup::"

echo "::group::fixture app image (SDK builder)"
app_src=tests/e2e/apps/e2e_probe
docker run --rm -v "$PWD":/src -w /src -u "$(id -u):$(id -g)" -e HOME=/tmp python:3.13-slim \
    sh -c "pip install --quiet --user build && python -m build --wheel --outdir tests/e2e/.work/dist $app_src >/dev/null" \
    || { echo "wheel build failed" >&2; exit 1; }
wheel=$(ls "$work"/dist/bdh_app_e2e_probe-*.whl)
bash "$sdk/builder/app_artifacts.sh" "$wheel" "$app_src/pyproject.toml" "$work/app" "$base"
version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$work/app/bdh-app.json")
image=$(push bdh-app-e2e_probe:candidate "${prefix}bdh-app-e2e_probe:$version")
# The supervisor must pull the app by digest from the registry.
docker rmi -f bdh-app-e2e_probe:candidate "${prefix}bdh-app-e2e_probe:$version" >/dev/null
echo "image: $image"
echo "::endgroup::"

echo "::group::catalog"
cp "$wheel" "$work/catalog/"
python3 - "$work/catalog/catalog.json" "$image" "$base" "$version" "$wheel" "$catalog_port" <<'EOF'
import hashlib, json, os, sys
from datetime import datetime, timezone

out, image, base, version, wheel, port = sys.argv[1:]
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
name = os.path.basename(wheel)
entry = {
    "version": version, "kind": "image", "hub_api": 2, "image": image, "base": base, "build": "e2e.1",
    "wheel_url": f"http://127.0.0.1:{port}/{name}",
    "sha256": hashlib.sha256(open(wheel, "rb").read()).hexdigest(),
    "permissions": [], "network": "none", "lifecycle": "on_demand", "memory_mb": 96,
    "companions": [], "provides": ["backgrounds"], "custom_base": False,
    "changelog": "stack-e2e fixture", "released_at": now, "built_at": now,
    "attestation": {"url": "", "signer_workflow": ""},
}
catalog = {"schema": 1, "apps": {"e2e_probe": {
    "name": "Probe Viewer", "description": "stack-e2e fixture",
    "repo": "blackdiamondlodge/BlackDiamondHub", "versions": [entry]}}}
with open(out, "w") as f:
    json.dump(catalog, f, indent=2)
EOF
cat "$work/catalog/catalog.json"
(cd "$work/catalog" && exec python3 -m http.server "$catalog_port" --bind 127.0.0.1 >"$work/catalog.log" 2>&1) &
echo $! > "$work/catalog.pid"
catalog_url=http://127.0.0.1:$catalog_port/catalog.json
wait_for 30 "catalog server" curl -fs "$catalog_url"
echo "::endgroup::"

echo "::group::postgres"
docker run -d --name bdh-e2e-db -p 127.0.0.1:5432:5432 \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=hub postgres:16 >/dev/null
wait_for 90 postgres docker exec bdh-e2e-db pg_isready -U postgres -h 127.0.0.1
echo "::endgroup::"

echo "::group::bdh-supervisor"
docker pull -q "$supervisor_image" >/dev/null
docker volume create bdh-e2e-socket >/dev/null
docker volume create bdh-e2e-state >/dev/null
# As in docker-compose.yml, firewall included; tests-only: http catalog, no attestation.
docker run -d --name bdh-e2e-supervisor --network host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v bdh-e2e-socket:/run/bdh-supervisor -v bdh-e2e-state:/state \
    -e BDH_SUPERVISOR_TOKEN="$token" \
    -e BDH_CATALOG_URL="$catalog_url" \
    -e BDH_IMAGE_PREFIX="$prefix" \
    -e BDH_ATTESTATION=off -e BDH_ALLOW_HTTP=1 \
    "$supervisor_image" >/dev/null
supervisor_up() {
    [ "$(docker inspect -f '{{.State.Running}}' bdh-e2e-supervisor)" = true ] || { docker logs bdh-e2e-supervisor >&2; exit 1; }
    docker run --rm -v bdh-e2e-socket:/s:ro busybox test -S /s/supervisor.sock
}
wait_for 120 "supervisor socket" supervisor_up
echo "::endgroup::"

echo "::group::hub image"
docker build -q --build-arg HUB_SITE=e2e -t "$hub_image" . >/dev/null
echo "::endgroup::"

echo "::group::hub"
docker run -d --name bdh-e2e-hub --network host \
    -e DJANGO_SECRET_KEY=stack-e2e-only \
    -e ALLOWED_HOSTS=localhost \
    -e HUB_SITE=e2e \
    -e DB_HOST=127.0.0.1 -e DB_NAME=hub -e DB_USER=postgres -e DB_PASSWORD=postgres \
    -e BROKER_BIND=172.17.0.1 -e APP_PORTS=4 \
    -e BDH_BROKER_URL=http://172.17.0.1:8099/_broker \
    -e BDH_SUPERVISOR_SOCKET=/run/bdh-supervisor/supervisor.sock \
    -e BDH_SUPERVISOR_TOKEN="$token" \
    -e BDH_CATALOG_URL="$catalog_url" \
    -v bdh-e2e-socket:/run/bdh-supervisor \
    "$hub_image" >/dev/null
hub_up() {
    [ "$(docker inspect -f '{{.State.Running}}' bdh-e2e-hub)" = true ] || { docker logs bdh-e2e-hub >&2; exit 1; }
    curl -fs "http://localhost:$hub_port/healthz"
}
wait_for 180 hub hub_up
echo "::endgroup::"

echo "::group::install e2e_probe"
docker exec bdh-e2e-hub /app/.venv/bin/python manage.py app_install e2e_probe --timeout 180 | tee "$work/install.txt"
app_port=$(sed -n 's/.* on port \([0-9]*\) .*/\1/p' "$work/install.txt" | head -n1)
[ -n "$app_port" ] || { echo "no app port in app_install output" >&2; exit 1; }
cat > "$work/stack.env" <<EOF
HUB_URL=http://localhost:$hub_port
APP_URL=http://localhost:$app_port
APP_ID=e2e_probe
APP_CONTAINER=bdh-app-e2e_probe
HUB_CONTAINER=bdh-e2e-hub
E2E_ARTIFACTS=$work/artifacts
EOF
cat "$work/stack.env"
echo "::endgroup::"
