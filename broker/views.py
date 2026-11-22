"""The broker's RPC endpoint and the hub pages that frame container apps."""
import json
import logging

import requests
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils.encoding import escape_uri_path, iri_to_uri
from django.views.decorators.csrf import csrf_exempt

from . import lifecycle, remote, rpc as ops, starting, visitor
from .models import AppInstall, AuditEntry
from .ratelimit import limiter
from .tokens import authenticate, bearer

log = logging.getLogger(__name__)

RAW_TYPES = ("image/", "audio/", "video/")
RAW_TIMEOUT = (3.0, 15.0)
RAW_HEADERS = ("Content-Length", "Cache-Control", "ETag", "Last-Modified")
RAW_CACHE_MAX = 256 * 1024
STARTING_RETRY = 5
RAW_CACHE_SECONDS = 7 * 24 * 3600
# Query flag on a /_raw/ URL whose fetch may wake a sleeping on-demand app (the
# saver's live cam). Stripped before the request reaches the app.
RAW_WAKE = "_bdh_wake"
# The hub pages an app may send the visitor to by name (see embed-host.js NAV_NAMES).
NAV_NAMES = frozenset({"landing_page", "broker:shell", "broker:shell_path", "hub_settings_app"})


# --- RPC -------------------------------------------------------------------------

def _error(code, message, status):
    return JsonResponse({"ok": False, "error": {"code": code, "message": message}}, status=status)


def _audit(install_id, op, allowed, detail=""):
    try:
        AuditEntry.record(install_id, op, allowed, str(detail)[:500])
    except Exception:
        log.exception("audit failed")


@csrf_exempt
def rpc(request, op):
    """``POST /_broker/rpc/<op>`` with ``Authorization: Bearer <app token>``."""
    if request.method != "POST":
        return _error("invalid", "POST only", 405)
    install = authenticate(bearer(request.META.get("HTTP_AUTHORIZATION")))
    if install is None:
        _audit("?", op, False, "bad token")
        return _error("forbidden", "unknown or disabled app token", 403)
    lifecycle.seen(install)
    fn = ops.OPS.get(op)
    if fn is None:
        return _error("unknown_op", f"no op {op!r}", 404)
    limit = ops.max_body(op)
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return _error("too_large", "request too large", 413)
    # read() rather than .body: DATA_UPLOAD_MAX_MEMORY_SIZE is for forms, uploads are bigger.
    raw = request.read(limit + 1)
    if len(raw) > limit:
        return _error("too_large", "request too large", 413)
    if not limiter.allow(install.app_id, op):
        _audit(install.app_id, op, False, "rate limited")
        return _error("rate_limited", "slow down", 429)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return _error("invalid", "body must be JSON", 400)
    if not isinstance(payload, dict):
        return _error("invalid", "body must be a JSON object", 400)
    try:
        result = fn(install, install.grants, payload)
    except ops.OpError as exc:
        if exc.code == "forbidden" or op in ops.AUDITED:
            detail = f"{exc.code}: {exc}"
            if op in ops.SECRET_OPS:
                detail += " " + _summary(op, payload)
            _audit(install.app_id, op, exc.code != "forbidden", detail)
        return _error(exc.code, str(exc), exc.status)
    except Exception as exc:
        log.exception("broker op %s for %s failed", op, install.app_id)
        return _error("error", type(exc).__name__, 500)
    if op in ops.AUDITED:
        _audit(install.app_id, op, True, _summary(op, payload))
    return JsonResponse({"ok": True, "result": result}, encoder=DjangoJSONEncoder)


def _summary(op, payload):
    if op in ops.SECRET_OPS:
        name = payload.get("name")
        return json.dumps({"name": name if isinstance(name, str) else None})[:500]
    keys = ("domain", "service", "entity_id", "key", "topic", "patterns", "filename", "message",
            "provider", "name", "mime", "main", "expected_revision")
    return json.dumps({k: payload[k] for k in keys if k in payload}, default=str)[:500]


# --- the hub's pages for container apps --------------------------------------------

class AppStarting(Exception):
    pass


def _spec(app_id):
    spec = remote.spec_for(app_id)
    if spec is None:
        if AppInstall.objects.filter(app_id=app_id, enabled=True).exists():
            raise AppStarting(app_id)
        raise Http404("No such app")
    return spec


def _starting(request, app_id):
    """An installed app whose manifest isn't in yet (it's starting or updating):
    a page that reloads itself, so a wall display isn't stranded on a 404."""
    from .proxy import display_name

    install = AppInstall.objects.filter(app_id=app_id).first()
    name = display_name(install) if install else app_id
    resp = HttpResponse(starting.page(name, STARTING_RETRY), status=503)
    resp["Retry-After"] = str(STARTING_RETRY)
    return resp


def _app_src(request, spec, path):
    path = "/" + path.lstrip("/")
    if path.startswith("/_bdh"):
        raise Http404("No such page")
    query = request.META.get("QUERY_STRING", "")
    return (remote.app_origin(spec.port, request.get_host()) + escape_uri_path(path)
            + ("?" + iri_to_uri(query) if query else ""))


def _title(spec, request):
    """The navbar title until the app sends its own: the best-matching tile's."""
    tiles = sorted(spec.tiles, key=lambda t: len(t.url), reverse=True)
    tile = next((t for t in tiles if request.path.startswith(t.url)), None) or next(iter(tiles), None)
    return tile.title if tile else spec.id


def shell(request, app_id, path=""):
    """``/apps/<app>/<path>``: the hub's chrome around the app's page."""
    try:
        spec = _spec(app_id)
    except AppStarting:
        return _starting(request, app_id)
    src = _app_src(request, spec, path)
    return render(request, "broker/shell.html", {
        "spec": spec, "src": src, "app_origin": remote.app_origin(spec.port, request.get_host()),
        "title": _title(spec, request),
    })


def bare(request, app_id, path=""):
    """``/apps/<app>/_bare/<path>``: the app's page alone (alert popups)."""
    try:
        spec = _spec(app_id)
    except AppStarting:
        return _starting(request, app_id)
    return render(request, "broker/bare.html", {
        "spec": spec, "src": _app_src(request, spec, path),
        "app_origin": remote.app_origin(spec.port, request.get_host()),
        "title": _title(spec, request),
    })


def raw(request, app_id, path):
    """``/apps/<app>/_raw/<path>``: an image or sound from the app, on the hub's origin."""
    try:
        spec = _spec(app_id)
    except AppStarting:
        raise Http404("App is starting") from None
    path = "/" + path.lstrip("/")
    if path.startswith("/_bdh"):
        raise Http404("No such file")
    install = remote._install(spec.id)
    params = request.GET.copy()
    waking = params.pop(RAW_WAKE, None) is not None
    query = params.urlencode() if waking else request.META.get("QUERY_STRING", "")
    full = escape_uri_path(path) + ("?" + iri_to_uri(query) if query else "")
    from bdh_sdk.identity import signed_headers

    # An app's /static/ files (tile icons) don't change within a version, so the
    # hub keeps a copy: the home page can show them while an on-demand app sleeps.
    static = path.startswith("/static/")
    key = f"broker:raw:{spec.id}:{install.version}:{full}"
    if static:
        hit = cache.get(key)
        if hit is not None:
            return _raw_response(HttpResponse(hit["body"], content_type=hit["type"]), hit["headers"])

    def fetch():
        headers = signed_headers(remote._token(install), "GET", full, identity=visitor.for_request(request))
        return requests.get(remote._upstream(install) + full, headers=headers, timeout=RAW_TIMEOUT,
                            stream=True, allow_redirects=False)

    try:
        try:
            resp = fetch()
        except requests.ConnectionError:
            # Asleep: wake it for a static file (once per version, then cached)
            # or a flagged fetch (the saver's cam), but not for other media,
            # which status rows reference all day.
            if not (static or waking) or not remote._wake(install):
                raise
            resp = fetch()
    except (requests.RequestException, remote.RemoteError):
        raise Http404("App unavailable")
    content_type = resp.headers.get("Content-Type", "")
    if resp.status_code != 200 or not content_type.startswith(RAW_TYPES):
        resp.close()
        raise Http404("No such file")
    kept = {name: resp.headers[name] for name in RAW_HEADERS if resp.headers.get(name)}
    size = resp.headers.get("Content-Length", "")
    if static and size.isdigit() and int(size) <= RAW_CACHE_MAX:
        try:
            body = resp.content
        except requests.RequestException:
            raise Http404("App unavailable")
        cache.set(key, {"body": body, "type": content_type, "headers": kept}, RAW_CACHE_SECONDS)
        return _raw_response(HttpResponse(body, content_type=content_type), kept)
    return _raw_response(StreamingHttpResponse(resp.iter_content(64 * 1024), content_type=content_type), kept)


def _raw_response(out, headers):
    for name, value in headers.items():
        out[name] = value
    out["X-Content-Type-Options"] = "nosniff"
    out["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return out


def share_blob(request, sha):
    """``/apps/_share/<sha256>``: an image attached to a published share. Public,
    like the photos it is; nothing else (main files, unpublished blobs) is served."""
    from . import shares

    if request.method not in ("GET", "HEAD"):
        return HttpResponse(status=405, headers={"Allow": "GET, HEAD"})
    blob = shares.public_blob(sha)
    if blob is None:
        raise Http404("No such file")
    out = _raw_response(HttpResponse(bytes(blob.data), content_type=blob.mime), {})
    out["Cache-Control"] = "public, max-age=31536000, immutable"
    out["Cross-Origin-Resource-Policy"] = "cross-origin"
    return out


def navigate(request):
    """``/apps/_nav/?name=<url name>&args=[...]``: an app asked for a hub page by name."""
    name = request.GET.get("name", "")
    if name not in NAV_NAMES:
        raise Http404("No such page")
    try:
        args = json.loads(request.GET.get("args") or "[]")
        if not isinstance(args, list) or not all(isinstance(a, (str, int)) for a in args):
            raise ValueError
        # Not the broker's own pages under an app (_bare, _raw) or /apps/_nav itself.
        if any(isinstance(a, str) and a.lstrip("/").startswith("_") for a in args):
            raise ValueError
        url = reverse(name, args=args)
    except (ValueError, NoReverseMatch):
        raise Http404("No such page")
    return HttpResponseRedirect(url)
