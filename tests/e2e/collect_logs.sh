#!/usr/bin/env bash
# Save the stack's container logs and state to tests/e2e/.work/artifacts.
cd "$(dirname "$0")/../.."
out=tests/e2e/.work/artifacts
mkdir -p "$out"
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "$out/containers.txt" 2>&1
for name in $(docker ps -a --format '{{.Names}}' | grep -E '^bdh-'); do
    docker logs --timestamps "$name" > "$out/$name.log" 2>&1
    docker inspect "$name" > "$out/$name.inspect.json" 2>&1
done
cp tests/e2e/.work/install.txt tests/e2e/.work/catalog/catalog.json tests/e2e/.work/catalog.log \
    tests/e2e/.work/resolved.json tests/e2e/.work/fake_ha.log "$out/" 2>/dev/null
curl -fs http://127.0.0.1:8123/_calls > "$out/fake_ha_calls.json" 2>/dev/null
exit 0
