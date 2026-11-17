import struct
import zlib

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


def _png(width=64, height=36, rgb=(30, 90, 160)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


PNG = _png()


def index(request):
    return render(request, "e2e_probe/index.html")


@never_cache
def background(request):
    return HttpResponse(PNG, content_type="image/png")
