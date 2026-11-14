"""Client for bdh-supervisor, the service that runs container apps.

It listens on a unix socket shared through a docker volume
(``BDH_SUPERVISOR_SOCKET``) and wants ``Authorization: Bearer
$BDH_SUPERVISOR_TOKEN``. The hub tells it which app and version to run and
what the admin approved; the supervisor owns everything Docker.

Unset either setting and :func:`enabled` is False: the hub then runs without
container apps (and :class:`SupervisorDisabled` is raised if one is asked for).
"""
import httpx
from django.conf import settings

TIMEOUT = httpx.Timeout(5.0, connect=2.0)
# POST /apps/{id}/start?wait=true waits for the app's health check (30 s by default).
START_TIMEOUT = httpx.Timeout(45.0, connect=2.0)
BASE_URL = "http://supervisor"

# Tests swap in httpx.MockTransport instances.
transport = None
async_transport = None


class SupervisorError(Exception):
    """The supervisor answered with an error (``status`` is the HTTP status)."""

    def __init__(self, detail, status=None):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class SupervisorDisabled(SupervisorError):
    """BDH_SUPERVISOR_SOCKET or BDH_SUPERVISOR_TOKEN isn't set."""


class SupervisorUnavailable(SupervisorError):
    """The socket isn't there, refused the connection or timed out."""


class NotFound(SupervisorError):
    pass


class Conflict(SupervisorError):
    pass


class Invalid(SupervisorError):
    pass


def socket_path():
    return getattr(settings, "BDH_SUPERVISOR_SOCKET", "") or ""


def _token():
    return getattr(settings, "BDH_SUPERVISOR_TOKEN", "") or ""


def enabled():
    return bool(socket_path() and _token())


def _check():
    if not enabled():
        raise SupervisorDisabled("the supervisor isn't configured (BDH_SUPERVISOR_SOCKET/BDH_SUPERVISOR_TOKEN)")


def _headers():
    return {"Authorization": f"Bearer {_token()}"}


def _detail(resp):
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or f"HTTP {resp.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail or body)[:300]


def _result(resp, text=False):
    if resp.status_code < 400:
        if text:
            return resp.text
        try:
            return resp.json()
        except ValueError as exc:
            raise SupervisorError("supervisor reply isn't JSON", resp.status_code) from exc
    if resp.status_code in (401, 403):
        raise SupervisorError(f"supervisor refused the token (HTTP {resp.status_code})", resp.status_code)
    cls = {404: NotFound, 409: Conflict, 422: Invalid}.get(resp.status_code, SupervisorError)
    raise cls(_detail(resp), resp.status_code)


def _sync_client(timeout):
    tr = transport or httpx.HTTPTransport(uds=socket_path())
    return httpx.Client(transport=tr, base_url=BASE_URL, timeout=timeout, headers=_headers())


def _async_client(timeout):
    tr = async_transport or httpx.AsyncHTTPTransport(uds=socket_path())
    return httpx.AsyncClient(transport=tr, base_url=BASE_URL, timeout=timeout, headers=_headers())


def request(method, path, *, json=None, params=None, timeout=TIMEOUT, text=False):
    _check()
    try:
        with _sync_client(timeout) as client:
            resp = client.request(method, path, json=json, params=params)
    except (httpx.TransportError, OSError) as exc:
        raise SupervisorUnavailable(f"supervisor unreachable: {exc}") from exc
    return _result(resp, text)


async def arequest(method, path, *, json=None, params=None, timeout=TIMEOUT):
    _check()
    try:
        async with _async_client(timeout) as client:
            resp = await client.request(method, path, json=json, params=params)
    except (httpx.TransportError, OSError) as exc:
        raise SupervisorUnavailable(f"supervisor unreachable: {exc}") from exc
    return _result(resp)


# --- the API ---------------------------------------------------------------

def healthz():
    return request("GET", "/healthz")


def list_apps():
    return request("GET", "/apps")["apps"]


def put_app(app_id, *, version, token, slot, network, allow_lan=False, time_zone="UTC", hub_brand=None):
    body = {"version": version, "token": token, "slot": slot, "network": network,
            "allow_lan": bool(allow_lan), "time_zone": time_zone, "hub_brand": hub_brand or {}}
    return request("PUT", f"/apps/{app_id}", json=body)


def get_app(app_id):
    return request("GET", f"/apps/{app_id}")


def delete_app(app_id, purge_data=False):
    return request("DELETE", f"/apps/{app_id}", params={"purge_data": "true" if purge_data else "false"})


def start(app_id, wait=True):
    return request("POST", f"/apps/{app_id}/start", params={"wait": "true" if wait else "false"},
                   timeout=START_TIMEOUT if wait else TIMEOUT)


def stop(app_id):
    return request("POST", f"/apps/{app_id}/stop")


def restart(app_id):
    return request("POST", f"/apps/{app_id}/restart", timeout=START_TIMEOUT)


def activity(app_id):
    return request("POST", f"/apps/{app_id}/activity")


def health(app_id):
    return request("GET", f"/apps/{app_id}/health")


def logs(app_id, lines=200):
    return request("GET", f"/apps/{app_id}/logs", params={"lines": int(lines)}, text=True)


def stats(app_id):
    return request("GET", f"/apps/{app_id}/stats")


def refresh_runtime():
    return request("POST", "/runtime/refresh", timeout=START_TIMEOUT)


async def astart(app_id, wait=True):
    return await arequest("POST", f"/apps/{app_id}/start", params={"wait": "true" if wait else "false"},
                          timeout=START_TIMEOUT if wait else TIMEOUT)


async def aactivity(app_id):
    return await arequest("POST", f"/apps/{app_id}/activity")
