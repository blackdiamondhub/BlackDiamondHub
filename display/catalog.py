"""What the screensaver can show, from the apps that provide it (``hub.provides``).

Info-strip slots store qualified stat ids (``"<app>.<key>"``), and the
screen saver qualified source ids (``"<app>.<key>"``) next to its own
``stock``, ``photos`` and ``mix``.
"""
from bdh_sdk.provides import LIVE, SEASON
from bdh_sdk.settings import app_setting

from hub import provides

from .options import APP

# Stats the display works out itself.
LOCAL = {"days_until_lifts": "Days until lifts open"}


def owner(key):
    return key.split(".", 1)[0] if "." in key else None


def season_apps():
    """Labels of the apps that can bring a season forward."""
    return [provides.app_label(s) for s in provides.providers(SEASON)]


def stat_groups(current=()):
    """``[(group label, [(id, label)])]`` for the info-strip selects.

    Saved ids nobody offers any more stay selectable, marked unavailable, so
    saving the page never drops them.
    """
    groups = {}
    for qid, label, app in provides.stat_catalogue():
        groups.setdefault(app, []).append((qid, label))
    if season_apps():
        groups.setdefault("Screensaver", []).extend(LOCAL.items())
    out = list(groups.items())
    known = {qid for _, items in out for qid, _ in items} | {"none"}
    missing = sorted({c for c in current if c and c not in known})
    if missing:
        out.append(("Unavailable", [(c, f"{c} (unavailable)") for c in missing]))
    return out


STOCK = {"id": "stock", "title": "Stock illustration", "description": "Paintings for the season", "kind": "stock"}
PHOTOS = {"id": "photos", "title": "My photos", "description": "From your photo library", "kind": "photos"}
BUILTIN = ("stock", "photos", "mix")


def _join(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " & " + parts[-1]


def saver_choices(current=(), sources=None):
    """The screen saver's sources for the settings page, in order.

    ``[{"id", "title", "description", "kind", "available"[, "app_label"]}]``:
    stock, My photos, every app's source in plugin order, saved ids nobody
    offers any more (kept so saving the page doesn't drop them), then Mix.
    """
    sources = provides.saver_sources() if sources is None else sources
    out = [dict(STOCK, available=True), dict(PHOTOS, available=True)]
    out += [{"id": s["id"], "title": s["title"], "description": s["description"], "kind": s["kind"],
             "app_label": s["app_label"], "available": True} for s in sources]
    known = {c["id"] for c in out} | {"mix"}
    for missing in sorted({c for c in current if c and c not in known}):
        out.append({"id": missing, "title": f"{missing} (unavailable)", "description": "No app offers this now",
                    "kind": "", "available": False})
    mix = ["Paintings", "photos"] + [s["title"] for s in sources]
    out.append({"id": "mix", "title": "Mix", "description": _join(mix), "kind": "mix", "available": True})
    return out


def resolve_source(saved, sources=None):
    """A season's saved source id, with names from before apps declared their own sources."""
    saved = (saved or "").strip() or "stock"
    if saved in BUILTIN:
        return saved
    sources = provides.saver_sources() if sources is None else sources
    live = [s for s in sources if s["kind"] == LIVE]
    if saved == "cam":
        return live[0]["id"] if live else "stock"
    app, _, key = saved.partition(".")
    if key == "backgrounds":
        mine = [s for s in live if s["app"] == app]
        if mine:
            return mine[0]["id"]
    return saved


def resolve_pick(sources=None):
    """The chosen live picture (``"<source id>/<image id>"``), or "" to rotate through them all.

    Until ``saver_pick`` is saved, the old ``cam`` setting (``"<app>.<camera>"``) picks.
    """
    raw = app_setting(APP, "saver_pick", None)
    if raw is not None:
        return str(raw).strip()
    old = str(app_setting(APP, "cam", "") or "").strip()
    app, _, slug = old.partition(".")
    if not slug:
        return ""
    sources = provides.saver_sources() if sources is None else sources
    live = next((s for s in sources if s["app"] == app and s["kind"] == LIVE), None)
    return f"{live['id']}/{slug}" if live else ""