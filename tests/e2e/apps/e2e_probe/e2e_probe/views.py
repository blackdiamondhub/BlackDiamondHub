import json
import os
import struct
import time
import urllib.request
import zlib
from pathlib import Path

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


def _png(width=64, height=36, rgb=(30, 90, 160)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


PNG = _png()


def _data():
    return Path(os.environ.get("BDH_DATA_DIR", "/data"))


def index(request):
    return render(request, "e2e_probe/index.html")


@never_cache
def background(request):
    return HttpResponse(PNG, content_type="image/png")


# The rest is only used by variants (see variants.py).

@never_cache
def files(request, name):
    """GET reads /data/files/<name>; ?write=<value> writes it first."""
    path = _data() / "files" / name
    if "write" in request.GET:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.GET["write"])
    if not path.exists():
        return JsonResponse({"found": False}, status=404)
    return JsonResponse({"found": True, "value": path.read_text()})


@never_cache
def events_log(request):
    path = _data() / "events.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return JsonResponse({"events": [json.loads(line) for line in lines]})


def record_event(event):
    with open(_data() / "events.jsonl", "a") as fh:
        fh.write(json.dumps({"topic": event.topic, "data": event.data}) + "\n")


@never_cache
def companion(request):
    url = os.environ.get("BDH_GO2RTC_URL", "")
    if not url:
        return JsonResponse({"url": None}, status=404)
    with urllib.request.urlopen(url.rstrip("/") + "/api/streams", timeout=5) as resp:
        return JsonResponse({"url": url, "reply": json.loads(resp.read())})


@never_cache
def status_log(request):
    path = _data() / "status.log"
    return JsonResponse({"calls": path.read_text().split() if path.exists() else []})


def status(mode=None):
    from bdh_sdk.plugins import Status

    from .plugin import VARIANT

    path = _data() / "status.log"
    with open(path, "a") as fh:
        fh.write(f"{time.time():.3f}\n")
    return Status(text=f"probe status #{len(path.read_text().split())}", refresh=(VARIANT or {}).get("refresh"))


@never_cache
def rpc(request, op):
    """Call broker op ``op`` with the JSON payload in ?p= through the SDK client."""
    from bdh_sdk import rpc as sdk_rpc

    payload = json.loads(request.GET.get("p") or "{}")
    try:
        result = sdk_rpc.client().call(op, payload)
    except sdk_rpc.BrokerError as exc:
        return JsonResponse({"ok": False, "code": exc.code, "status": exc.status, "error": str(exc)})
    return JsonResponse({"ok": True, "result": result})
