#!/usr/bin/env bash
# Overlay a bdh-sdk checkout onto an app runtime image, checked the way
# bdh-supervisor's runtime/Dockerfile checks the SDK it pins:
#
#   bash tests/e2e/sdk_runtime.sh <bdh-sdk dir> <runtime image> <output tag>
set -euo pipefail
sdk=$(cd "$1" && pwd)
runtime=$2
tag=$3
ctx=$(mktemp -d -p "${RUNNER_TEMP:-$PWD}")
trap 'rm -rf "$ctx"' EXIT
docker run --rm -v "$sdk":/sdk:ro -v "$ctx":/out -u "$(id -u):$(id -g)" -e HOME=/tmp python:3.13-slim \
    sh -c "pip install --quiet --user build && cp -r /sdk /tmp/sdk && python -m build --wheel --outdir /out /tmp/sdk >/dev/null"
cat > "$ctx/Dockerfile" <<'EOF'
ARG RUNTIME
FROM ${RUNTIME}
USER root
COPY *.whl /tmp/
RUN pip install --no-deps --force-reinstall /tmp/bdh_sdk-*.whl \
    && rm /tmp/*.whl \
    && pip check \
    && pip list --format=freeze > /opt/bdh/constraints.txt \
    && python -m compileall -q -j0 /usr/local/lib/python3.13 \
    && python /opt/bdh/check.py base \
    && python -c "import bdh_sdk.plugins as p; api = int(open('/opt/bdh/hub_api').read()); assert p.API_VERSION == api, (p.API_VERSION, api)"
USER 10001:10001
EOF
docker build -q --build-arg RUNTIME="$runtime" -t "$tag" "$ctx" >/dev/null
echo "runtime with candidate SDK: $tag ($(ls "$ctx"/*.whl | xargs -n1 basename))"
