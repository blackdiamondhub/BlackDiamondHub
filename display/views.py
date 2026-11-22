import json
import logging
import re
import secrets
from datetime import timedelta

from bdh_sdk.plugins import BOOL, SECRET
from bdh_sdk.provides import LIVE
from bdh_sdk.qr import qr_svg
from bdh_sdk.storage import app_state
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from hub import provides
from broker.views import RAW_WAKE
from hub_settings import store, vault
from hub_settings.views import _render, _save, saved_redirect, staff_page

from . import catalog, options
from .library import photo_json, season_photos
from .models import DisplayLoad, DisplaySample, Heartbeat, SaverPhoto, UploadLink
from .photos import BadPhoto, add_photo
from .options import APP, BUILTIN_SOURCES, SEASON_IDS, SEASONS, SLOTS, STOCK_EXTRAS, STOCK_STYLES, value
from .schedule import is_night, lodge_minute, parse_hhmm, window
from .season import by_date, lodge_today, opening_day, parse_mmdd, resolve, winter_signal
from .stats import collect

# Pictures Mix takes from apps' sources between them, so one big album can't swamp the reply.
MIX_IMAGES = 200
log = logging.getLogger(__name__)


def stock_art(season):
    names = (*STOCK_STYLES, *STOCK_EXTRAS.get(season, ()))
    return [static(f"display/stock/{season}/{name}.webp") for name in names]


def season_info(today):
    """``(signal, opens)``: whether an app says winter has come (None when no app can
    tell), and the winter opening day an app announces (None if none)."""
    from bdh_sdk.provides import SEASON

    if not provides.providers(SEASON):
        return None, None
    hints = provides.season_hints()
    starts = options.starts()
    return winter_signal(hints, today, starts), opening_day(hints, today, starts)


def season_signal(today=None):
    """Whether an app says winter has come early (None when no app can tell)."""
    return season_info(today or lodge_today())[0]


def current_season(today=None, signal=None):
    signal = season_signal() if signal is None else signal
    return resolve(today or lodge_today(), options.starts(), signal,
                   value("winter_on_lifts"), app_state(APP))


def _waking(url):
    """The saver shows the cam for as long as it's up, so its fetches may wake an
    on-demand app (``/_raw/`` media otherwise leaves a sleeping app asleep)."""
    if not (url.startswith("/apps/") and "/_raw/" in url):
        return url
    return url + ("&" if "?" in url else "?") + RAW_WAKE + "=1"


def _preview(request, source_ids):
    """Unsaved settings-page values a staff preview may try out: source, pick, rotate, caption."""
    if not (request.user.is_authenticated and request.user.is_staff):
        return {}
    out = {}
    if request.GET.get("source") in source_ids:
        out["source"] = request.GET["source"]
    if "pick" in request.GET:
        out["pick"] = request.GET["pick"][:200]
    try:
        out["rotate"] = min(90, max(15, int(request.GET["rotate"])))
    except (KeyError, ValueError):
        pass
    if request.GET.get("caption") in ("0", "1"):
        out["caption"] = request.GET["caption"] == "1"
    return out


def _layer(src, images, pick, captions):
    """An app source as the saver draws it: ``{"id", "kind", "title", "items", "pick"}``.

    A live source with a picked picture sends only that one; otherwise the saver rotates through them.
    """
    items = []
    for img in images:
        item = {"id": img["id"], "url": img["url"], "label": img.get("label") or ""}
        if src["kind"] == LIVE:
            item["url"] = _waking(item["url"])
            item["refresh_seconds"] = img.get("refresh_seconds")
        else:
            item["caption"] = (img.get("caption") or "") if captions else ""
        items.append(item)
    chosen = None
    source_id, _, image_id = (pick or "").partition("/")
    if src["kind"] == LIVE and source_id == src["id"] and image_id:
        one = [i for i in items if i["id"] == image_id]
        if one:
            chosen, items = image_id, one
    return {"id": src["id"], "kind": src["kind"], "title": src["title"], "items": items, "pick": chosen}


def _share(layers, total):
    """Trims Mix's app layers to ``total`` pictures between them, taken from each in turn."""
    take = [0] * len(layers)
    while total > 0 and any(n < len(layer["items"]) for n, layer in zip(take, layers)):
        for i, layer in enumerate(layers):
            if total > 0 and take[i] < len(layer["items"]):
                take[i] += 1
                total -= 1
    for n, layer in zip(take, layers):
        layer["items"] = layer["items"][:n]


@require_GET
def saver_api(request):
    today = lodge_today()
    starts = options.starts()
    signal, opens = season_info(today)
    season = request.GET.get("season")
    if season not in SEASON_IDS:
        season = current_season(today, signal)
    sources = provides.saver_sources()
    by_id = {s["id"]: s for s in sources}
    preview = _preview(request, set(BUILTIN_SOURCES) | set(by_id))

    chosen = preview.get("source") or catalog.resolve_source(value(f"source_{season}"), sources)
    if chosen == "mix":
        wanted = ["stock", "photos", *by_id]
    else:
        wanted = [chosen] if chosen in ("stock", "photos") or chosen in by_id else []
    app_ids = [w for w in wanted if w in by_id]
    captions = preview.get("caption", value("photo_caption"))
    pick = preview["pick"] if "pick" in preview else catalog.resolve_pick(sources)
    images = provides.saver_images(app_ids) if app_ids else {}
    layers = [layer for layer in (_layer(by_id[i], images.get(i) or (), pick, captions) for i in app_ids)
              if layer["items"]]
    if chosen == "mix":
        _share(layers, MIX_IMAGES)
    photos = season_photos(season, captions) if "photos" in wanted else []
    shown = {layer["id"] for layer in layers} | {"stock"} | ({"photos"} if photos else set())
    order = [w for w in wanted if w in shown] or ["stock"]

    keys = [options.slot(season, n) for n in range(1, SLOTS + 1)]
    response = JsonResponse({
        "season": season,
        "label": dict(SEASONS)[season],
        "sources": order,
        "rotate_seconds": preview.get("rotate", value("rotate_seconds")),
        "layers": layers,
        "photos": photos,
        "stock": stock_art(season),
        "burn_in": value("burn_in"),
        "idle_minutes": value("idle_minutes"),
        "night": window(),
        "stats": collect(keys, today, starts, signal, opens),
    })
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def schedule_api(request):
    """Night mode for Home Assistant: ``dark`` is true while the display should be off."""
    night = window()
    response = JsonResponse({
        "enabled": night is not None,
        "from": value("night_from") if night else None,
        "until": value("night_until") if night else None,
        "dark": bool(night) and is_night(lodge_minute(), night["from"], night["until"]),
    })
    response["Cache-Control"] = "no-store"
    return response


HEARTBEAT_KEEP = timedelta(days=7)


@csrf_exempt
@require_POST
def heartbeat(request):
    """A hub page checking in (every page sends one a minute)."""
    now = timezone.now()
    Heartbeat.objects.update_or_create(
        ip=request.META.get("REMOTE_ADDR") or "0.0.0.0",
        defaults={"last_seen": now, "path": request.POST.get("path", "")[:200],
                  "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200]},
    )
    Heartbeat.objects.filter(last_seen__lt=now - HEARTBEAT_KEEP).delete()
    return HttpResponse(status=204)


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@csrf_exempt
@require_POST
def page_load(request):
    """A hub page just loaded on a screen; records how the page before it ended."""
    post = request.POST
    now = timezone.now()
    clean = post.get("prev_clean")
    DisplayLoad.objects.create(
        ip=request.META.get("REMOTE_ADDR") or "0.0.0.0", at=now,
        path=post.get("path", "")[:200], nav_type=post.get("nav_type", "")[:20],
        referrer=post.get("referrer", "")[:200],
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:200],
        heap_mb=_int(post.get("heap_mb")), prev_path=post.get("prev_path", "")[:200],
        prev_clean={"1": True, "0": False}.get(clean),
        prev_lived_s=_int(post.get("prev_lived_s")), prev_silent_s=_int(post.get("prev_silent_s")),
        prev_heap_mb=_int(post.get("prev_heap_mb")), prev_stats=_json_object(post.get("prev_stats")),
    )
    DisplayLoad.objects.filter(at__lt=now - HEARTBEAT_KEEP).delete()
    return HttpResponse(status=204)


SAMPLE_KEEP = timedelta(days=3)
SAMPLE_MAX_BYTES = 16 * 1024


def _json_object(value):
    """``value`` parsed as a JSON object of at most SAMPLE_MAX_BYTES, else None."""
    if not value or len(value) > SAMPLE_MAX_BYTES:
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@csrf_exempt
@require_POST
def sample(request):
    """A screen's page-health telemetry (sent every 30 s by every hub page)."""
    data = _json_object(request.POST.get("data"))
    if data is None:
        return HttpResponse(status=400)
    now = timezone.now()
    DisplaySample.objects.create(ip=request.META.get("REMOTE_ADDR") or "0.0.0.0", at=now,
                                 path=request.POST.get("path", "")[:200], data=data)
    DisplaySample.objects.filter(at__lt=now - SAMPLE_KEEP).delete()
    return HttpResponse(status=204)


@require_GET
def samples_api(request):
    """Recent telemetry, newest first (``?ip=``, ``?since=`` ISO time, ``?limit=`` up to 5000)."""
    rows = DisplaySample.objects.order_by("-at", "-id")
    if request.GET.get("ip"):
        rows = rows.filter(ip=request.GET["ip"])
    since = parse_datetime(request.GET.get("since") or "")
    if since:
        rows = rows.filter(at__gte=since)
    limit = min(max(_int(request.GET.get("limit")) or 200, 1), 5000)
    response = JsonResponse({"samples": [
        {"ip": s.ip, "at": s.at.isoformat(), "path": s.path, "data": s.data} for s in rows[:limit]
    ]})
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def page_loads_api(request):
    """Recent page loads, newest first (``?ip=``, ``?limit=``); ``crash`` marks an unclean end."""
    loads = DisplayLoad.objects.order_by("-at", "-id")
    if request.GET.get("ip"):
        loads = loads.filter(ip=request.GET["ip"])
    limit = min(max(_int(request.GET.get("limit")) or 100, 1), 1000)
    response = JsonResponse({"loads": [
        {"ip": d.ip, "at": d.at.isoformat(), "path": d.path, "nav_type": d.nav_type,
         "referrer": d.referrer, "user_agent": d.user_agent, "heap_mb": d.heap_mb,
         "prev_path": d.prev_path, "prev_clean": d.prev_clean, "prev_lived_s": d.prev_lived_s,
         "prev_silent_s": d.prev_silent_s, "prev_heap_mb": d.prev_heap_mb,
         "prev_stats": d.prev_stats, "crash": d.prev_clean is False}
        for d in loads[:limit]
    ]})
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@require_POST
def sleep_api(request):
    """The screensaver showing at night: turn this screen off if it's a UniFi Connect one."""
    from . import connect

    try:
        asleep = connect.sleep(request.META.get("REMOTE_ADDR"))
    except connect.ConnectError as exc:
        log.warning("%s", exc)
        asleep = False
    return JsonResponse({"asleep": asleep})


@require_GET
def heartbeats_api(request):
    """Last check-in per client IP, for the screen watchdog."""
    now = timezone.now()
    response = JsonResponse({"clients": [
        {"ip": h.ip, "last_seen": h.last_seen.isoformat(),
         "age_seconds": int((now - h.last_seen).total_seconds()),
         "path": h.path, "user_agent": h.user_agent}
        for h in Heartbeat.objects.order_by("ip")
    ]})
    response["Cache-Control"] = "no-store"
    return response


def _spec():
    from .plugin import spec

    return spec


def _form_values(post):
    values, errors, raw = {}, {}, {}
    for setting in options.SETTINGS:
        if setting.type == BOOL:
            raw[setting.key] = setting.key in post
        elif setting.type == SECRET:  # never echoed back into the page
            try:
                values[setting.key] = setting.parse(post.get(setting.key, ""))
            except ValueError as exc:
                errors[setting.key] = str(exc)
            continue
        else:
            raw[setting.key] = post.get(setting.key, "")
        try:
            values[setting.key] = setting.parse(raw[setting.key])
            if setting.key.endswith("_start"):
                month, day = parse_mmdd(values[setting.key])
                values[setting.key] = f"{month:02d}-{day:02d}"
            elif setting.key in ("night_from", "night_until"):
                minute = parse_hhmm(values[setting.key])
                values[setting.key] = f"{minute // 60:02d}:{minute % 60:02d}"
            if setting.key.startswith("slot_"):
                values[setting.key] = _slot_value(setting.key, values[setting.key])
        except ValueError as exc:
            errors[setting.key] = str(exc)
    return values, errors, raw


SLOT_ID = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)?$")


def _slot_value(key, posted):
    """A posted stat id; the site default is stored as empty so it follows the installed apps."""
    _, season, n = key.split("_")
    posted = (posted or "").strip()
    if not posted or posted == options.default_slot(season, int(n)):
        return ""
    if not SLOT_ID.match(posted):
        raise ValueError("Pick a stat from the list")
    return posted


def _current(spec, saved):
    # Secrets are write-only: never in the page's values.
    return {s.key: saved.get(s.key, store.fallback(spec.id, s)) for s in spec.settings if s.type != SECRET}


def _save_pick(request, spec, pick):
    """Always store the pick, even "" (rotate): ``_save`` skips defaults, and a missing
    ``saver_pick`` would fall back to the retired ``cam`` setting. Then retire ``cam``."""
    from hub_settings.models import AppSetting

    AppSetting.objects.update_or_create(app_id=spec.id, key="saver_pick",
                                        defaults={"value": pick, "updated_by": request.user})
    AppSetting.objects.filter(app_id=spec.id, key="cam").delete()


@staff_page
@sensitive_post_parameters("connect_password")
def settings_page(request):
    spec = _spec()
    saved = store.saved(spec.id)
    errors, status = {}, 200
    sources = provides.saver_sources()

    if request.method == "POST":
        values, errors, raw = _form_values(request.POST)
        _check_saver(values, errors, saved, sources)
        if not errors:
            _save(request, spec, saved, values)
            _save_pick(request, spec, values["saver_pick"])
            tab = request.POST.get("tab")
            return saved_redirect(request, f"{request.path}?saved=1" + (f"&season={tab}" if tab in SEASON_IDS else ""))
        current, status = {**_current(spec, saved), **raw}, 400
    else:
        current = _current(spec, saved)
        if "saver_pick" not in saved:
            current["saver_pick"] = catalog.resolve_pick(sources)

    showing = current_season_safe()
    selected = request.GET.get("season") if request.GET.get("season") in SEASON_IDS else showing

    photos = list(SaverPhoto.objects.all())
    seasons = [{
        "id": sid, "label": label, "source": catalog.resolve_source(current[f"source_{sid}"], sources),
        "error": errors.get(f"source_{sid}", ""),
        "photo_count": sum(1 for p in photos if p.enabled and p.in_season(sid)),
        "start": current[f"{sid}_start"], "start_key": f"{sid}_start",
        "start_error": errors.get(f"{sid}_start", ""),
        "slots": [{"n": n, "key": f"slot_{sid}_{n}", "value": options.slot(sid, n, current[f"slot_{sid}_{n}"])}
                  for n in range(1, SLOTS + 1)],
    } for sid, label in SEASONS]

    live = [s["id"] for s in sources if s["kind"] == LIVE]
    images = provides.saver_images(live) if live else {}
    choices = _controls(catalog.saver_choices([s["source"] for s in seasons], sources), images,
                        current["saver_pick"])
    chosen = next((s["source"] for s in seasons if s["id"] == selected), "stock")
    for c in choices:
        c["selected"] = c["id"] == chosen
    in_use = [slot["value"] for season in seasons for slot in season["slots"]]
    previews = {c["id"]: {"title": c["title"], "kind": c["kind"], "items": c["items"]} for c in choices if c["items"]}
    return _render(
        request, "display/settings.html", "display",
        v=current, errors=errors, seasons=seasons, selected=selected,
        showing=dict(SEASONS)[showing], choices=choices,
        chosen=next((c for c in choices if c["selected"]), choices[0]),
        slot_groups=catalog.stat_groups(in_use), season_apps=catalog.season_apps(), saver_previews=previews,
        photos=[photo_json(p) for p in photos], season_choices=SEASONS,
        stock={sid: stock_art(sid) for sid in SEASON_IDS},
        home_bgs=options.HOME_BACKGROUNDS, home_tiles=options.HOME_TILES, home_pictures=home_pictures(),
        has_errors=bool(errors), just_saved=request.GET.get("saved") == "1", status=status,
        connect_password_set="connect_password" in vault.names(spec.id),
    )


INTROS = {
    "stock": "Paintings for the season, slowly panning, one after another.",
    "photos": "Shows your library photos for this season, slowly panning, one after another.",
    "mix": "Takes turns between the paintings, your photos and each app's pictures.",
}


def _controls(choices, images, pick):
    """Adds what each source's controls need: its intro, its pictures and whether it rotates or captions."""
    for c in choices:
        items = [{"id": i["id"], "value": f"{c['id']}/{i['id']}", "label": i.get("label") or i["id"],
                  "url": _waking(i["url"]) if c["kind"] == LIVE else i["url"]} for i in images.get(c["id"]) or ()]
        c["items"] = items
        c["pick"] = pick if pick.startswith(c["id"] + "/") else ""
        c["pick_missing"] = bool(c["pick"]) and c["pick"].partition("/")[2] not in {i["id"] for i in items}
        if c["kind"] == LIVE:
            c["intro"] = "Live pictures that refresh as each camera updates." + (
                " Pick one, or rotate through all of them." if len(items) > 1 else "")
        elif c["kind"] == "photos" and c.get("app_label"):
            c["intro"] = f"Photos from the {c['app_label']} app, slowly panning, one after another."
        elif not c["available"]:
            c["intro"] = "No app offers this now, so the screen saver shows the paintings instead."
        else:
            c["intro"] = INTROS[c["id"]]
        c["picker"] = c["kind"] == LIVE and len(items) > 1
        c["rotates"] = c["available"] and (c["kind"] != LIVE or (len(items) > 1 and not c["pick"]))
        c["captions"] = c["kind"] in ("photos", "mix")
    return choices


def _check_saver(values, errors, saved, sources):
    """Season sources must be ones on offer (or what's already saved); a picture must be a live source's."""
    offered = set(BUILTIN_SOURCES) | {s["id"] for s in sources}
    for sid in SEASON_IDS:
        key = f"source_{sid}"
        picked = (values.get(key) or "").strip() or "stock"
        if picked not in offered and picked != saved.get(key):
            errors[key] = "Pick a screen saver source from the list"
        values[key] = picked
    pick = (values.get("saver_pick") or "").strip()
    live = {s["id"] for s in sources if s["kind"] == LIVE}
    source_id, _, image_id = pick.partition("/")
    if pick and pick != saved.get("saver_pick") and not (source_id in live and image_id and len(pick) <= 200):
        errors["saver_pick"] = "Pick a picture from the list"
    values["saver_pick"] = pick


CLAIM_COOKIE = "display_upload"
CLAIMED_MINUTES = 30


def _seasons(raw):
    picked = [s for s in (raw or "").split(",") if s in SEASON_IDS]
    return [] if len(picked) == len(SEASON_IDS) else picked


def _add_files(request, link=None):
    seasons = _seasons(request.POST.get("seasons"))
    files = request.FILES.getlist("photo")
    if not files:
        return JsonResponse({"error": "No photo was sent."}, status=400)
    added = []
    for upload in files:
        try:
            added.append(photo_json(add_photo(upload, seasons, link)))
        except BadPhoto as exc:
            return JsonResponse({"error": str(exc), "photos": added}, status=400)
    return JsonResponse({"photos": added})


@staff_page
@require_POST
def link_create(request):
    UploadLink.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    link = UploadLink.objects.create()
    url = request.build_absolute_uri(reverse("display_upload", args=[link.key]))
    return JsonResponse({"key": link.key, "url": url, "qr": qr_svg(url), "seconds": link.seconds_left()})


@staff_page
@require_GET
def photo_list(request):
    payload = {"photos": [photo_json(p) for p in SaverPhoto.objects.all()]}
    link = UploadLink.objects.filter(key=request.GET.get("link", "")).first()
    if link:
        payload["link"] = {"seconds": link.seconds_left(), "claimed": bool(link.claim),
                           "arrived": link.photos.count()}
    return JsonResponse(payload)


@staff_page
@require_POST
def photo_add(request):
    return _add_files(request)


@staff_page
@require_POST
def photo_update(request, photo_id):
    photo = get_object_or_404(SaverPhoto, pk=photo_id)
    if "enabled" in request.POST:
        photo.enabled = request.POST["enabled"] in ("1", "true", "on")
    if "seasons" in request.POST:
        photo.seasons = _seasons(request.POST["seasons"])
    photo.save()
    return JsonResponse(photo_json(photo))


@staff_page
@require_POST
def photo_delete(request, photo_id):
    get_object_or_404(SaverPhoto, pk=photo_id).delete()
    return JsonResponse({"ok": True})


def _link_for(request, key):
    """The upload link if this phone may use it, else (None, status, message)."""
    link = UploadLink.objects.filter(key=key).first()
    if not link:
        return None, 404, "This upload link isn't valid. Scan the code on the hub again."
    if not link.live():
        return None, 410, "This upload code has expired. Make a new one from the hub's Settings."
    if link.claim and not secrets.compare_digest(link.claim, request.COOKIES.get(CLAIM_COOKIE, "")):
        return None, 403, "This code is already in use on another phone. Make a new one from the hub."
    return link, 200, ""


def upload_page(request, key):
    link, status, message = _link_for(request, key)
    if not link:
        return render(request, "display/upload.html", {"message": message}, status=status)
    fresh = not link.claim
    if fresh:
        link.claim = secrets.token_urlsafe(24)
        link.expires_at = timezone.now() + timedelta(minutes=CLAIMED_MINUTES)
        link.save(update_fields=["claim", "expires_at"])
    response = render(request, "display/upload.html", {
        "link": link, "season_choices": SEASONS, "current": current_season_safe(),
        "post_url": reverse("display_upload_photo", args=[link.key]),
    })
    if fresh:
        response.set_cookie(CLAIM_COOKIE, link.claim, max_age=CLAIMED_MINUTES * 60, httponly=True,
                            samesite="Lax")
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def upload_photo(request, key):
    link, status, message = _link_for(request, key)
    if not link or not link.claim:
        return JsonResponse({"error": message or "Open the upload page first."}, status=status if not link else 403)
    return _add_files(request, link)


def current_season_safe():
    try:
        return current_season()
    except Exception:
        return by_date(lodge_today(), options.starts())


HOME_DEFAULT = {"bg": None, "tiles": "solid", "dim": 45, "shade": "0.45"}


def home_picture(mode, today=None):
    """Today's home-screen picture for a background mode; None for the drawn ridge.

    The picture changes once a day: seasonal art, or the owner's photos for the
    season (art when there are none).
    """
    if mode not in ("art", "photos"):
        return None
    season = current_season_safe()
    pics = [p["url"] for p in season_photos(season)] if mode == "photos" else []
    pics = pics or stock_art(season)
    day = (today or lodge_today()).toordinal()
    return pics[day % len(pics)] if pics else None


def home_pictures():
    """What each background choice would show today, for the Settings preview."""
    try:
        return {mode: home_picture(mode) for mode, _ in options.HOME_BACKGROUNDS}
    except Exception:
        log.exception("home preview pictures failed")
        return {}


def home_theme(today=None):
    """What the home screen draws behind its tiles, and how the tiles look.

    Never raises, so the home page always renders.
    """
    try:
        tiles = value("home_tiles")
        dim = max(0, min(80, int(value("home_dim"))))
        bg = home_picture(value("home_bg"), today)
        return {"bg": bg, "tiles": tiles if tiles in dict(options.HOME_TILES) else "solid", "dim": dim,
                "shade": f"{dim / 100:.2f}"}
    except Exception:
        log.exception("home theme failed; using the default")
        return dict(HOME_DEFAULT)
