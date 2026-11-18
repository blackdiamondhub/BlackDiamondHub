#!/usr/bin/env bash
# Boot the hub + bdh-supervisor stack the way homehub runs it, with the
# e2e_probe fixture app installed as a container app, for stack_test.py:
#
#   BDH_SDK=.sdk bash tests/e2e/run_stack.sh
#
# - a local registry (localhost:5000) holding the app runtime base and the
#   fixture image built on it by the SDK's builder, both pinned by digest;
# - a catalog.json listing the fixture, served over http on the host;
# - bdh-supervisor and the hub image (HUB_SITE=e2e), both on the host network
#   sharing the API socket;
# - the fixture installed with `manage.py app_install`.
#
# Components not overridden come from stack.lock.json (HEADS=1: what the
# lock tracks). Overrides, e.g. candidates built by a caller's CI:
#   BDH_SDK           bdh-sdk checkout (builder; SDK_IN_RUNTIME=1 also
#                     installs it into the runtime) - required
#   SUPERVISOR_IMAGE  RUNTIME_IMAGE   local tags or registry refs; pulled
#                     only when not already present locally
#   HUB_SRC           hub checkout to build (default: this one), or
#   HUB_IMAGE         a prebuilt hub image
#   SUITE             core (default), caps or caps-life: also the capability tests'
#                     identities (variants.py), a fake Home Assistant and a
#                     short supervisor idle timeout
#   EXTRA_APP_DIR     an app under test: bdh-app.json, its wheel, image.tar
#                     (docker save of bdh-app-<id>:candidate) and base.txt
#                     (the runtime it was built on, used unless RUNTIME_IMAGE)
#
# Writes tests/e2e/.work/stack.env for the tests and .work/resolved.json,
# what actually ran. Containers are named bdh-e2e-* and bdh-app-*;
# tests/e2e/collect_logs.sh gathers their logs.
set -euo pipefail
cd "$(dirname "$0")/../.."
here=$PWD
sdk=$(cd "${BDH_SDK:?set BDH_SDK to a bdh-sdk checkout}" && pwd)
work=$here/tests/e2e/.work
registry=localhost:5000
prefix=$registry/blackdiamondlodge/
eval "$(python3 tests/e2e/lock.py resolve ${HEADS:+--heads} | sed 's/^/lock_/')"
extra=${EXTRA_APP_DIR:+$(cd "$EXTRA_APP_DIR" && pwd)}
runtime=${RUNTIME_IMAGE:-}
[ -n "$runtime" ] || { [ -z "$extra" ] || runtime=$(cat "$extra/base.txt"); }
runtime=${runtime:-$lock_RUNTIME_IMAGE}
supervisor_image=${SUPERVISOR_IMAGE:-$lock_SUPERVISOR_IMAGE}
hub_src=$(cd "${HUB_SRC:-.}" && pwd)
hub_image=${HUB_IMAGE:-}
catalog_port=8765
hub_port=8000
suite=${SUITE:-core}
ha_port=8123
token=$(openssl rand -hex 32)

if [ -n "$extra" ] && [ "${SDK_IN_RUNTIME:-}" = 1 ]; then
    echo "EXTRA_APP_DIR apps are built on their own base; SDK_IN_RUNTIME would change it" >&2
    exit 2
fi

wait_for() {  # <seconds> <what> <command...>
    local deadline=$((SECONDS + $1)) what=$2
    shift 2
    until "$@" >/dev/null; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "timed out after $1s waiting for $what" >&2
            return 1
        fi
        sleep 1
    done
    echo "ready: $what"
}

ensure() {  # <image>: use a local image (e.g. a caller's candidate) as is, else pull it
    docker image inspect "$1" >/dev/null 2>&1 || docker pull -q "$1" >/dev/null
}

push() {  # <local image> <repo:tag> -> repo@sha256:...
    docker tag "$1" "$2" && docker push -q "$2" >/dev/null
    docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$2" | grep "^${2%:*}@" | head -n1
}

rm -rf "$work" && mkdir -p "$work/catalog"

echo "::group::registry and runtime base"
docker run -d --name bdh-e2e-registry -p 127.0.0.1:5000:5000 registry:2 >/dev/null
wait_for 60 registry curl -fs "http://$registry/v2/"
ensure "$runtime"
base_src=$runtime
if [ "${SDK_IN_RUNTIME:-}" = 1 ]; then
    bash tests/e2e/sdk_runtime.sh "$sdk" "$runtime" bdh-e2e-runtime:sdk
    base_src=bdh-e2e-runtime:sdk
fi
base=$(push "$base_src" "${prefix}bdh-app-runtime:e2e")
echo "base: $base"
echo "::endgroup::"

echo "::group::fixture app images (SDK builder)"
srcs=(tests/e2e/apps/e2e_probe)
if [[ $suite == caps* ]]; then
    # The capability tests' identities: the same probe code, other manifests.
    python3 tests/e2e/variants.py "$work/variants" | tee "$work/variants.txt"
    while read -r _ _ dir; do srcs+=("${dir#"$here"/}"); done < "$work/variants.txt"
fi
docker run --rm -v "$PWD":/src -w /src -u "$(id -u):$(id -g)" -e HOME=/tmp python:3.13-slim \
    sh -c 'pip install --quiet --user build && for s; do python -m build --wheel --outdir "tests/e2e/.work/dist/${s##*/}" "$s" >/dev/null || exit 1; done' \
    sh "${srcs[@]}" || { echo "wheel build failed" >&2; exit 1; }

build_app() {  # <src dir>: build, push and record one fixture image
    local key=${1##*/} out wheel id version image
    out=$work/apps/$key
    wheel=$(ls "$work/dist/$key"/*.whl)
    bash "$sdk/builder/app_artifacts.sh" "$wheel" "$1/pyproject.toml" "$out" "$base" >"$out.log" 2>&1 \
        || { cat "$out.log" >&2; return 1; }
    read -r id version < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["id"], c["version"])' "$out/bdh-app.json")
    image=$(push "bdh-app-$id:candidate" "${prefix}bdh-app-$id:$version")
    # The supervisor must pull the app by digest from the registry.
    docker rmi -f "bdh-app-$id:candidate" "${prefix}bdh-app-$id:$version" >/dev/null
    echo "$out/bdh-app.json $image $wheel $id $version" > "$out.built"
    echo "$id $version: $image"
}

mkdir -p "$work/apps"
# One job per app id: versions of an id share the bdh-app-<id>:candidate tag.
declare -A by_id=()
for src in "${srcs[@]}"; do
    id=${src##*/}; id=${id%-*}
    by_id[$id]+="$src "
done
pids=()
for id in "${!by_id[@]}"; do
    (for src in ${by_id[$id]}; do build_app "$src" || exit 1; done) &
    pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid" || { echo "fixture build failed" >&2; exit 1; }; done
catalog_apps=()
image=
: > "$work/fixtures.txt"
for built in "$work"/apps/*.built; do
    read -r app_json app_image app_wheel id version < "$built"
    cat "$built" >> "$work/fixtures.txt"
    [ "$id" != e2e_probe ] || image=$app_image
    catalog_apps+=("$app_json" "$app_image" "$app_wheel" "$(python3 tests/e2e/catalog.py --name "$id")")
done
echo "::endgroup::"

extra_id= extra_image= extra_candidate=
if [ -n "$extra" ]; then
    echo "::group::app under test"
    read -r extra_id extra_version < <(python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["id"], c["version"])' "$extra/bdh-app.json")
    docker load -q -i "$extra/image.tar"
    extra_candidate=$(docker image inspect -f '{{.Id}}' "bdh-app-$extra_id:candidate")
    extra_image=$(push "bdh-app-$extra_id:candidate" "${prefix}bdh-app-$extra_id:$extra_version")
    docker rmi -f "bdh-app-$extra_id:candidate" "${prefix}bdh-app-$extra_id:$extra_version" >/dev/null
    extra_wheel=$(ls "$extra"/*.whl)
    extra_name=$(cat "$extra/name.txt" 2>/dev/null || echo "$extra_id" | sed 's/_/ /g; s/\b\(.\)/\u\1/g')
    catalog_apps+=("$extra/bdh-app.json" "$extra_image" "$extra_wheel" "$extra_name")
    echo "$extra_id $extra_version: $extra_image"
    echo "::endgroup::"
fi

echo "::group::catalog"
for ((i = 2; i < ${#catalog_apps[@]}; i += 4)); do cp "${catalog_apps[i]}" "$work/catalog/"; done
python3 tests/e2e/catalog.py "$work/catalog/catalog.json" "http://127.0.0.1:$catalog_port/" "$base" "${catalog_apps[@]}"
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

supervisor_env=() hub_env=(-e APP_PORTS=4)
if [[ $suite == caps* ]]; then
    echo "::group::fake Home Assistant"
    python3 tests/e2e/fake_ha.py "$ha_port" >"$work/fake_ha.log" 2>&1 &
    echo $! > "$work/fake_ha.pid"
    wait_for 30 "fake Home Assistant" curl -fs "http://127.0.0.1:$ha_port/_calls"
    echo "::endgroup::"
    # Idle apps stop after 75 s (the hub reports activity at most once a minute).
    supervisor_env=(-e BDH_IDLE_TIMEOUT=75)
    hub_env=(-e APP_PORTS=12 -e HOMEASSISTANT_URL="http://127.0.0.1:$ha_port" -e HOMEASSISTANT_ACCESS_TOKEN=fake-ha-token)
fi

echo "::group::bdh-supervisor"
ensure "$supervisor_image"
docker volume create bdh-e2e-socket >/dev/null
docker volume create bdh-e2e-state >/dev/null
# As in docker-compose.yml, firewall included; tests-only: http catalog, no attestation.
# The gateway and firewall helper run the supervisor image too: use the one under test.
docker run -d --name bdh-e2e-supervisor --network host \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v bdh-e2e-socket:/run/bdh-supervisor -v bdh-e2e-state:/state \
    -e BDH_SUPERVISOR_TOKEN="$token" \
    -e BDH_CATALOG_URL="$catalog_url" \
    -e BDH_IMAGE_PREFIX="$prefix" \
    -e BDH_ATTESTATION=off -e BDH_ALLOW_HTTP=1 \
    -e BDH_SUPERVISOR_IMAGE="$supervisor_image" "${supervisor_env[@]}" \
    "$supervisor_image" >/dev/null
supervisor_up() {
    [ "$(docker inspect -f '{{.State.Running}}' bdh-e2e-supervisor)" = true ] || { docker logs bdh-e2e-supervisor >&2; exit 1; }
    docker run --rm -v bdh-e2e-socket:/s:ro busybox test -S /s/supervisor.sock
}
wait_for 120 "supervisor socket" supervisor_up
echo "::endgroup::"

echo "::group::hub image"
hub_mounts=()
if [ -n "$hub_image" ]; then
    ensure "$hub_image"
    # A published image is built for another site; give it the e2e one.
    hub_mounts=(-v "$here/sites/e2e:/app/sites/e2e:ro")
else
    if [ "$hub_src" != "$here" ]; then
        rm -rf "$hub_src/sites/e2e" && cp -r sites/e2e "$hub_src/sites/"
    fi
    hub_image=bdh-e2e-hub
    docker build -q --build-arg HUB_SITE=e2e -t "$hub_image" "$hub_src" >/dev/null
fi
echo "::endgroup::"

echo "::group::hub"
docker run -d --name bdh-e2e-hub --network host \
    -e DJANGO_SECRET_KEY=stack-e2e-only \
    -e ALLOWED_HOSTS=localhost \
    -e HUB_SITE=e2e \
    -e DB_HOST=127.0.0.1 -e DB_NAME=hub -e DB_USER=postgres -e DB_PASSWORD=postgres \
    -e BROKER_BIND=172.17.0.1 "${hub_env[@]}" \
    -e BDH_BROKER_URL=http://172.17.0.1:8099/_broker \
    -e BDH_SUPERVISOR_SOCKET=/run/bdh-supervisor/supervisor.sock \
    -e BDH_SUPERVISOR_TOKEN="$token" \
    -e BDH_CATALOG_URL="$catalog_url" \
    -v bdh-e2e-socket:/run/bdh-supervisor "${hub_mounts[@]}" \
    "$hub_image" >/dev/null
hub_up() {
    [ "$(docker inspect -f '{{.State.Running}}' bdh-e2e-hub)" = true ] || { docker logs bdh-e2e-hub >&2; exit 1; }
    curl -fs "http://localhost:$hub_port/healthz"
}
wait_for 180 hub hub_up
echo "::endgroup::"

echo "::group::install apps"
docker exec bdh-e2e-hub /app/.venv/bin/python manage.py app_install e2e_probe --timeout 180 | tee "$work/install.txt"
app_port=$(sed -n 's/.* on port \([0-9]*\) .*/\1/p' "$work/install.txt" | head -n1)
[ -n "$app_port" ] || { echo "no app port in app_install output" >&2; exit 1; }
if [ -n "$extra_id" ]; then
    docker exec bdh-e2e-hub /app/.venv/bin/python manage.py app_install "$extra_id" --timeout 180 | tee -a "$work/install.txt"
fi
if [[ $suite == caps* ]]; then
    docker exec -i bdh-e2e-hub /app/.venv/bin/python manage.py shell < tests/e2e/install_caps.py | tee -a "$work/install.txt"
    grep -q '^caps installed$' "$work/install.txt" || { echo "capability fixtures not installed" >&2; exit 1; }
fi
cat > "$work/stack.env" <<EOF
HUB_URL=http://localhost:$hub_port
APP_URL=http://localhost:$app_port
APP_ID=e2e_probe
APP_CONTAINER=bdh-app-e2e_probe
HUB_CONTAINER=bdh-e2e-hub
EXTRA_APP_ID=$extra_id
SUITE=$suite
FAKE_HA_URL=http://127.0.0.1:$ha_port
BROKER_URL=http://172.17.0.1:8099/_broker
WORK_DIR=$work
E2E_ARTIFACTS=$work/artifacts
EOF
cat "$work/stack.env"
echo "::endgroup::"

echo "::group::resolved components"
sdk_version=$(docker run --rm --entrypoint python "$base" -c 'import importlib.metadata as m; print(m.version("bdh-sdk"))')
docker image inspect "$supervisor_image" "$runtime" "$hub_image" > "$work/images.json"
python3 - "$work/resolved.json" <<EOF
import json, subprocess, sys

def git(path):
    r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None

images = {i: img for i, img in zip(["supervisor", "runtime", "hub"], json.load(open("$work/images.json")))}

def image(key, ref, **more):
    img = images[key]
    labels = img.get("Config", {}).get("Labels") or {}
    return {"ref": ref, "id": img["Id"], "repo_digests": img.get("RepoDigests") or [],
            "revision": labels.get("org.opencontainers.image.revision"), **more}

resolved = {
    "harness": {"commit": git("$here"), "lock": json.load(open("$here/tests/e2e/stack.lock.json")),
                "heads": "${HEADS:-}" == "1"},
    "hub": image("hub", "$hub_image", source=None if "${HUB_IMAGE:-}" else "$hub_src",
                 commit=None if "${HUB_IMAGE:-}" else git("$hub_src")),
    "supervisor": image("supervisor", "$supervisor_image"),
    "runtime": image("runtime", "$runtime", registry_base="$base", sdk_overlay="${SDK_IN_RUNTIME:-}" == "1",
                     bdh_sdk_version="$sdk_version"),
    "sdk": {"path": "$sdk", "commit": git("$sdk")},
    "fixture": {"id": "e2e_probe", "image": "$image"},
    "suite": "$suite",
    "fixtures": [dict(zip(["id", "version", "image"], l.split()[3:5] + l.split()[1:2]))
                 for l in open("$work/fixtures.txt")],
    "app_under_test": {"id": "$extra_id", "image": "$extra_image", "candidate_id": "$extra_candidate"}
    if "$extra_id" else None,
}
json.dump(resolved, open(sys.argv[1], "w"), indent=2)
EOF
cat "$work/resolved.json"
echo "::endgroup::"
