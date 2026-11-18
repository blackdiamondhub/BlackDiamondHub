"""The stack-e2e lock file (stack.lock.json): what each component pins and tracks.

    python tests/e2e/lock.py resolve [--heads]   # KEY=VALUE lines for run_stack.sh
    python tests/e2e/lock.py bump                 # re-pin every component to what it tracks

Images are pinned by registry digest and track a tag; repos are pinned by
commit and track a ref. --heads resolves the tracked refs instead of the pins
(the non-blocking compat run). Stdlib only.
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

LOCK = Path(__file__).with_name("stack.lock.json")
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def image_digest(ref):
    """``ghcr.io/owner/name:tag`` -> its current ``sha256:...`` (anonymous pull)."""
    m = re.fullmatch(r"([^/]+)/(.+):([^:/@]+)", ref)
    if not m:
        raise SystemExit(f"not a registry/name:tag reference: {ref}")
    registry, name, tag = m.groups()
    with urllib.request.urlopen(f"https://{registry}/token?scope=repository:{name}:pull", timeout=30) as r:
        token = json.load(r)["token"]
    req = urllib.request.Request(f"https://{registry}/v2/{name}/manifests/{tag}", method="HEAD",
                                 headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers["Docker-Content-Digest"]


def commit(repo, ref):
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", ref, f"{ref}^{{}}"],
                         check=True, capture_output=True, text=True).stdout.split("\n")
    shas = {line.split("\t")[1]: line.split("\t")[0] for line in out if "\t" in line}
    # An annotated tag's commit is the peeled ^{} entry.
    sha = next((v for k, v in shas.items() if k.endswith("^{}")), None) or next(iter(shas.values()), None)
    if not sha:
        raise SystemExit(f"{repo} has no ref {ref}")
    return sha


def resolve(lock, heads):
    sup, rt, sdk = lock["supervisor"], lock["runtime"], lock["sdk"]
    return {
        "SUPERVISOR_IMAGE": sup["tracks"] if heads else sup["image"],
        "RUNTIME_IMAGE": rt["tracks"] if heads else rt["image"],
        "SDK_REPO": sdk["repo"],
        "SDK_REF": sdk["tracks"] if heads else sdk["commit"],
    }


def bump(lock):
    for key in ("supervisor", "runtime"):
        tracks = lock[key]["tracks"]
        lock[key]["image"] = f"{tracks.rsplit(':', 1)[0]}@{image_digest(tracks)}"
    lock["sdk"]["commit"] = commit(lock["sdk"]["repo"], lock["sdk"]["tracks"])
    return lock


def main(argv):
    lock = json.loads(LOCK.read_text())
    if argv[:1] == ["resolve"]:
        for k, v in resolve(lock, "--heads" in argv).items():
            print(f"{k}={v}")
    elif argv[:1] == ["bump"]:
        LOCK.write_text(json.dumps(bump(lock), indent=2) + "\n", newline="\n")
        print(LOCK.read_text(), end="")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
