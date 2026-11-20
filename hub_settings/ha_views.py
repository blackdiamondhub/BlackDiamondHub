"""Settings → Home Assistant: the instances apps reach through ``bdh_sdk.ha``.

Tokens are write-only: the page only says whether one is set. A saved token is
only ever sent to the URL it was saved with, so changing an instance's URL (or
testing a new one) needs the token entered again.
"""

from urllib.parse import urlsplit
import time

import requests
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import ha, vault
from .models import HAInstance
from .views import _render, saved_redirect, staff_page

NEW = "new"
TEST_TIMEOUT = 8
STATUS_TIMEOUT = 4
STATUS_CACHE = 20  # seconds a REST probe's answer is reused
LIVE_STATES = {"connected", "reconnecting", "unreachable", "bad_token", "no_token"}
_probes = {}  # instance id -> (monotonic time, url, state)


def _clean_url(raw):
    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 200:
        return None
    if parts.query or parts.fragment:
        return None
    return url


def _taken_ids():
    """Ids an instance can't use: other event sources and app ids (an app may
    publish under its own id, so it could fake that instance's events)."""
    from hub import events
    from hub.modes import plugins

    return events.reserved_names() | {s.id for s in plugins()} | {"hub"}


def _blank_new(rows):
    if rows:
        return {"id": "", "name": "", "site": "", "url": "", "error": {}}
    return {"id": ha.DEFAULT_ID, "name": "Home Assistant", "site": "", "url": "", "error": {}}


def _cards(rows, posted=None, errors=None):
    tokens = vault.names(ha.VAULT_OWNER)
    default = next((r.id for r in rows if r.is_default), rows[0].id if rows else None)
    errors = errors or {}
    chosen = (posted or {}).get("_default") or default
    cards = []
    for r in rows:
        p = (posted or {}).get(r.id, {})
        cards.append({
            "id": r.id, "name": p.get("name", r.name), "site": p.get("site", r.site),
            "url": p.get("url", r.url), "default": r.id == chosen,
            "token_set": ha.token_name(r.id) in tokens, "error": errors.get(r.id, {}),
        })
    return cards


def _page(request, rows, posted=None, errors=None, new=None, status=200, message=""):
    return _render(
        request, "hub_settings/ha.html", "ha",
        cards=_cards(rows, posted, errors), new=new or _blank_new(rows),
        has_errors=bool(errors) or bool(message), message=message,
        just_saved=request.GET.get("saved") == "1", vault_ok=vault.available(), status=status,
    )


def _check(fields, errors, *, new_id=None, taken=()):
    """Validate one instance's posted fields in place; returns the cleaned dict."""
    out = {"name": fields.get("name", "").strip()[:80], "site": fields.get("site", "").strip()[:80]}
    if new_id is not None:
        if not ha.ID_RE.match(new_id):
            errors["id"] = "Lowercase letters, digits and _; starts with a letter."
        elif new_id in taken:
            errors["id"] = "That id is already used."
    if not out["name"]:
        errors["name"] = "Give it a name."
    url = _clean_url(fields.get("url"))
    if url is None:
        errors["url"] = "An http:// or https:// address, like http://homeassistant.local:8123."
    out["url"] = url or fields.get("url", "").strip()
    return out


def _after_change():
    ha.clear()
    _probes.clear()
    from hub import events

    events.sync_instances()


@staff_page
@sensitive_post_parameters()
@sensitive_variables("posted", "token", "new_token", "tokens")
def ha_settings(request):
    rows = list(HAInstance.objects.order_by("order", "name"))
    if request.method != "POST":
        return _page(request, rows)

    post = request.POST
    by_id = {r.id: r for r in rows}

    remove = post.get("remove")
    if remove:
        if remove in by_id:
            with transaction.atomic():
                by_id[remove].delete()
                vault.delete_secret(ha.VAULT_OWNER, ha.token_name(remove))
                left = HAInstance.objects.order_by("order", "name")
                if left and not any(r.is_default for r in left):
                    HAInstance.objects.filter(pk=left[0].pk).update(is_default=True)
            _after_change()
        return saved_redirect(request, f"{request.path}?saved=1")
    clear = post.get("clear_token")
    if clear:
        if clear in by_id:
            vault.delete_secret(ha.VAULT_OWNER, ha.token_name(clear))
            _after_change()
        return saved_redirect(request, f"{request.path}?saved=1")
    errors, posted, tokens = {}, {"_default": post.get("default")}, {}
    taken = _taken_ids() | set(by_id)
    for r in rows:
        err = {}
        fields = {k: post.get(f"{r.id}__{k}", "") for k in ("name", "site", "url")}
        posted[r.id] = _check(fields, err)
        token = post.get(f"{r.id}__token", "")
        if token:
            tokens[r.id] = token
        elif "url" not in err and posted[r.id]["url"] != r.url:
            err["token"] = "Enter the token again for a new address."
        if err:
            errors[r.id] = err

    new_id = post.get(f"{NEW}__id", "").strip()
    new_fields = {k: post.get(f"{NEW}__{k}", "") for k in ("name", "site", "url")}
    new = None
    if new_id or new_fields["url"].strip() or not rows:
        err = {}
        new = _check(new_fields, err, new_id=new_id, taken=taken)
        new.update(id=new_id, error=err)
        new_token = post.get(f"{NEW}__token", "")
        if new_token:
            tokens[new_id] = new_token
        else:
            err["token"] = "Enter a long-lived access token."
        if err:
            errors[NEW] = err

    if tokens and not vault.available():
        message = "The secret store has no key on this hub; see the README."
        return _page(request, rows, posted, errors, new, status=400, message=message)
    for instance_id, token in tokens.items():
        try:
            from bdh_sdk.secrets import check_value

            check_value(token)
        except ValueError as exc:
            if new is not None and instance_id == new_id:
                new["error"]["token"] = str(exc)
                errors[NEW] = new["error"]
            else:
                errors.setdefault(instance_id, {})["token"] = str(exc)
    if errors:
        return _page(request, rows, posted, errors, new, status=400)

    default = post.get("default")
    if default == NEW:
        default = new_id
    ids = [r.id for r in rows] + ([new_id] if new else [])
    if default not in ids:
        default = next((r.id for r in rows if r.is_default), ids[0] if ids else None)
    with transaction.atomic():
        for r in rows:
            p = posted[r.id]
            r.name, r.site, r.url = p["name"], p["site"], p["url"]
            r.is_default = r.id == default
            r.updated_by = request.user
            r.save()
        if new:
            HAInstance.objects.create(
                id=new_id, name=new["name"], site=new["site"], url=new["url"], is_default=new_id == default,
                order=len(rows), updated_by=request.user)
        for instance_id, token in tokens.items():
            vault.set_secret(ha.VAULT_OWNER, ha.token_name(instance_id), token, user=request.user)
    _after_change()
    return saved_redirect(request, f"{request.path}?saved=1")


@staff_page
@require_POST
@sensitive_post_parameters()
@sensitive_variables("token")
def ha_test(request):
    """Try an instance's address and token: the saved token only for its saved address."""
    instance_id = request.POST.get("id", "")
    url = _clean_url(request.POST.get("url"))
    if url is None:
        return JsonResponse({"ok": False, "message": "That isn't an http:// or https:// address."})
    token = request.POST.get("token", "")
    if not token:
        row = HAInstance.objects.filter(pk=instance_id).first()
        if row is not None and row.url.rstrip("/") == url:
            token = ha._token(row.id)
    if not token:
        return JsonResponse({"ok": False, "message": "Enter the access token to test this address."})
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(f"{url}/api/config", headers=headers, timeout=TEST_TIMEOUT)
    except requests.RequestException as exc:
        return JsonResponse({"ok": False, "message": f"Can't reach it ({type(exc).__name__})."})
    if resp.status_code == 401:
        return JsonResponse({"ok": False, "message": "Home Assistant refused the token."})
    if resp.status_code != 200:
        return JsonResponse({"ok": False, "message": f"Home Assistant answered HTTP {resp.status_code}."})
    try:
        conf = resp.json()
    except ValueError:
        return JsonResponse({"ok": False, "message": "That doesn't look like Home Assistant."})
    if not isinstance(conf, dict):
        return JsonResponse({"ok": False, "message": "That doesn't look like Home Assistant."})
    count = None
    try:
        states = requests.get(f"{url}/api/states", headers=headers, timeout=TEST_TIMEOUT)
        if states.status_code == 200 and isinstance(states.json(), list):
            count = len(states.json())
    except (requests.RequestException, ValueError):
        pass
    name = str(conf.get("location_name") or "Home Assistant")[:80]
    ver = str(conf.get("version") or "")[:20]
    msg = f"Connected to {name}" + (f" ({ver})" if ver else "")
    if count is not None:
        msg += f", {count} entities"
    return JsonResponse({"ok": True, "message": msg + "."})


@sensitive_variables("token")
def _probe(row):
    """Ask Home Assistant's REST API directly, for an instance with no live connection here."""
    hit = _probes.get(row.id)
    now = time.monotonic()
    if hit and hit[1] == row.url and now - hit[0] < STATUS_CACHE:
        return hit[2]
    token = ha._token(row.id)
    if not token:
        state = "no_token"
    else:
        try:
            resp = requests.get(f"{row.url.rstrip('/')}/api/config",
                                headers={"Authorization": f"Bearer {token}"}, timeout=STATUS_TIMEOUT)
            state = {200: "connected", 401: "bad_token", 403: "bad_token"}.get(resp.status_code, "unreachable")
        except requests.RequestException:
            state = "unreachable"
    _probes[row.id] = (now, row.url, state)
    return state


@staff_page
@require_GET
def ha_status(request):
    """Each saved instance's connection state, for the dot in its card's title bar.

    The live event-source connection when there is one; otherwise (nobody
    subscribed, or not the server process) a quick REST check.
    """
    from hub import events

    out = {}
    for row in HAInstance.objects.order_by("order", "name"):
        state = events.instance_status(row.id)
        out[row.id] = state if state in LIVE_STATES else _probe(row)
    return JsonResponse({"instances": out})