"""The feedback pill and modal in the top bar, and where they send messages.

Feedback is handled by the ``feedback`` container app (bdh-app-feedback). The
hub posts messages to it (adding the page and the stay in progress, which the
app can't see) and shows the unread count the app keeps in its app state.

Without the app installed and enabled, the pill, the menu item and the inbox
link are hidden.
"""
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from .modes import current_stay, plugins

log = logging.getLogger(__name__)

APP_ID = "feedback"
# Where the container app takes messages and shows its inbox (its URLs are
# mounted at its prefix, ``feedback/``).
APP_SUBMIT_PATH = "/feedback/submit/"
APP_INBOX_PATH = "/feedback/"
# The app's state key holding the unread count, kept up to date by the app.
UNREAD_KEY = "unread_count"
# Identity flag that tells the app a message comes from the hub's submit
# endpoint. The proxy builds visitor identities itself, so visitors can't set it.
SUBMIT_FLAG = "feedback_submit"


def container_enabled():
    """The feedback container app is installed and enabled."""
    if "broker" not in settings.INSTALLED_APPS:
        return False
    from broker.remote import spec_for

    try:
        return spec_for(APP_ID) is not None
    except Exception:
        log.exception("feedback app lookup failed")
        return False


def _is_staff(user):
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


def _app_unread():
    from hub_settings.state import DatabaseStateBackend

    try:
        count = DatabaseStateBackend().get(APP_ID, UNREAD_KEY)
    except KeyError:
        return 0
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def unread_count(user):
    return _app_unread() if _is_staff(user) else 0


def context(request):
    """``hub_feedback``: the URLs the top bar uses and the unread count, or
    None when the feedback app isn't installed."""
    if not container_enabled():
        return {"hub_feedback": None}
    from broker.remote import page_url

    return {"hub_feedback": {
        "submit_url": reverse("hub_feedback_submit"),
        "count_url": reverse("hub_feedback_unread"),
        "inbox_url": page_url(APP_ID, APP_INBOX_PATH),
        "unread": unread_count(getattr(request, "user", None)),
    }}


def stay_label(request):
    """"the <guest> stay" / "an owner visit" for the stay in progress, else ""."""
    try:
        stay = current_stay(request)
    except Exception:
        log.exception("stay lookup failed")
        return ""
    if not stay:
        return ""
    if stay.get("owner"):
        return "an owner visit"
    guest = (stay.get("guest") or "").strip()
    return f"the {guest} stay"[:120] if guest else ""


def page_info(page_url):
    """Title and tile icon of the hub page a message was sent from."""
    from broker.templatetags.broker_tags import tile_icon

    path = urlsplit(page_url or "").path or ""
    if path in ("", "/"):
        return {"title": "Home", "icon": ""}
    best, best_len = None, -1
    for spec in plugins():
        if not (spec.urls or getattr(spec, "remote", False)) or not spec.tiles:
            continue
        prefix = "/" + spec.prefix
        if path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = spec, len(prefix)
    if best is None:
        return {"title": path, "icon": ""}
    tile = best.tiles[0]
    try:
        icon = tile_icon(tile.icon)
    except Exception:
        icon = ""
    return {"title": tile.title, "icon": icon}


@require_POST
def submit(request):
    """Pass a message from the top bar's modal on to the feedback app."""
    if not container_enabled():
        return JsonResponse({"success": False, "error": "Feedback is off"}, status=404)
    message = (request.POST.get("message") or "").strip()
    if not message:
        return JsonResponse({"success": False, "error": "Empty message"}, status=400)
    page_url = (request.POST.get("page_url") or "")[:200]
    page = page_info(page_url)
    data = {
        "message": message,
        "page_url": page_url,
        "page_title": page["title"][:120],
        "page_icon": page["icon"][:300],
        "stay": stay_label(request),
    }
    from broker import remote, visitor

    identity = {**visitor.for_request(request), SUBMIT_FLAG: 1}
    try:
        resp = remote.post_form(APP_ID, APP_SUBMIT_PATH, data, identity=identity)
    except remote.RemoteError as exc:
        log.warning("feedback not delivered: %s", exc)
        return JsonResponse({"success": False, "error": "Feedback app unavailable"}, status=502)
    if resp.status_code != 200:
        log.warning("feedback not delivered: HTTP %s", resp.status_code)
        return JsonResponse({"success": False, "error": "Feedback app unavailable"}, status=502)
    return JsonResponse({"success": True})


def unread(request):
    """The badge count for the top bar (0 for anyone but staff)."""
    return JsonResponse({"unread_feedback_count": unread_count(request.user)})
