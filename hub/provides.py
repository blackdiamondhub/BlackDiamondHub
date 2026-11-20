"""Data apps provide to the rest of the hub (``PluginSpec.provides``).

Providers run in the same worker pool as status hooks, with a short deadline,
and their results are cached; a failing or slow provider serves its last good
result, and one that has never answered is simply left out. Results are
checked here, so a consumer can trust the shapes it gets.
"""
import logging
from datetime import timezone
from functools import partial

from bdh_sdk.hooks import saver_value
from bdh_sdk.provides import (
    CAPABILITIES, LIVE, MAX_SAVER_IMAGES, SAVER, SAVER_TEXT_MAX, SEASON, STATS, SeasonHint, Stat,
)

from .modes import plugins
from .status import _converted, _gather

logger = logging.getLogger(__name__)

TTL = 60
TIMEOUT = 2.0

GRACE = 0.3

_warned = set()


def app_label(spec):
    return spec.tiles[0].title if spec.tiles else spec.id.replace("_", " ").title()


def providers(capability):
    """Installed specs providing ``capability``, in plugin order."""
    out = []
    for spec in plugins():
        provides = getattr(spec, "provides", None) or {}
        for name in provides:
            if name not in CAPABILITIES and (spec.id, name) not in _warned:
                _warned.add((spec.id, name))
                logger.info("%s provides unknown capability %r; ignored", spec.id, name)
        if capability in provides:
            out.append(spec)
    return out


def _iso(when):
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stats_value(allowed, result):
    if _converted(result):
        # Re-check against the declared keys: the app's reply is untrusted.
        return {k: v for k, v in (result.value or {}).items() if k in allowed and isinstance(v, dict)}
    out = {}
    for stat in list(result or ()):
        if not isinstance(stat, Stat):
            raise TypeError(f"stats provider returned {type(stat).__name__}")
        if stat.key not in allowed:
            logger.warning("Skipping undeclared stat %r", stat.key)
            continue
        if stat.key in out:
            continue
        item = {"label": stat.label, "order": stat.order}
        if stat.time is not None:
            item["time"] = _iso(stat.time)
        else:
            item["value"] = str(stat.value)
        out[stat.key] = item
    return out


def _saver_value(sources, result):
    if not _converted(result):
        return saver_value(sources, result)
    # A container app's reply is untrusted: check it again against what it declared.
    kinds = {src.key: src.kind for src in sources}
    out, seen, counts = [], set(), {}
    for img in result.value or ():
        if not isinstance(img, dict):
            continue
        source, image_id, url = img.get("source"), img.get("id"), img.get("url")
        kind = kinds.get(source)
        if kind is None or not isinstance(image_id, str) or not image_id or len(image_id) > SAVER_TEXT_MAX \
                or not isinstance(url, str) or not url:
            continue
        if (source, image_id) in seen or counts.get(source, 0) >= MAX_SAVER_IMAGES:
            continue
        item = {"source": source, "id": image_id, "url": url,
                "label": str(img.get("label") or "")[:SAVER_TEXT_MAX],
                "caption": str(img.get("caption") or "")[:SAVER_TEXT_MAX]}
        if kind == LIVE:
            refresh = img.get("refresh_seconds")
            if isinstance(refresh, bool) or not isinstance(refresh, int) or refresh < 1:
                continue
            item["refresh_seconds"] = refresh
        seen.add((source, image_id))
        counts[source] = counts.get(source, 0) + 1
        out.append(item)
    return out


def _season_value(result):
    if _converted(result):
        return result.value
    if result is None:
        return None
    if not isinstance(result, SeasonHint):
        raise TypeError(f"season provider returned {type(result).__name__}")
    out = {"season": result.season, "reason": result.reason}
    if getattr(result, "opens_on", None):
        out["opens_on"] = result.opens_on.isoformat()
    return out


def _run(capability, specs, convert, timeout):
    kind = f"provide:{capability}"
    jobs = [(kind, s.id, s.provides[capability], convert(s), TTL) for s in specs]
    results = _gather(jobs, timeout, grace=min(GRACE, timeout)) if jobs else {}
    return [(s, results[(kind, s.id)]["value"]) for s in specs
            if (kind, s.id) in results and not results[(kind, s.id)].get("missing")]


def stat_catalogue():
    """``[(qualified id, label, app label)]`` for every declared stat, without calling providers."""
    return [(f"{s.id}.{key}", label, app_label(s))
            for s in providers(STATS) for key, label in s.stat_keys]


def stat_owners():
    """Plugin ids that provide stats."""
    return {s.id for s in providers(STATS)}


def stats(plugin_ids=None, timeout=TIMEOUT):
    """``{"<plugin>.<key>": {"label", "value" | "time", "order"}}`` from stat providers.

    With ``plugin_ids``, only those providers are called.
    """
    specs = [s for s in providers(STATS) if plugin_ids is None or s.id in plugin_ids]
    allowed = lambda s: partial(_stats_value, frozenset(k for k, _ in s.stat_keys))  # noqa: E731
    out = {}
    for spec, value in _run(STATS, specs, allowed, timeout):
        for key, item in (value or {}).items():
            out[f"{spec.id}.{key}"] = item
    return out


def saver_sources():
    """Every declared screen saver source, in plugin order, without calling providers.

    ``[{"id": "<plugin>.<key>", "app", "app_label", "key", "title", "description", "kind"}]``
    """
    return [{"id": f"{s.id}.{src.key}", "app": s.id, "app_label": app_label(s), "key": src.key,
             "title": src.title, "description": src.description, "kind": src.kind}
            for s in providers(SAVER) for src in getattr(s, "saver_sources", None) or ()]


def saver_images(source_ids=None, timeout=TIMEOUT):
    """``{"<plugin>.<key>": [{"id", "url", "label", "caption"[, "refresh_seconds"]}]}``.

    With ``source_ids``, only the apps offering those sources are called.
    """
    wanted = None if source_ids is None else set(source_ids)
    specs = [s for s in providers(SAVER)
             if wanted is None or any(f"{s.id}.{src.key}" in wanted for src in s.saver_sources or ())]
    convert = lambda s: partial(_saver_value, tuple(s.saver_sources or ()))  # noqa: E731
    out = {}
    if not specs:
        return out
    for spec, value in _run(SAVER, specs, convert, timeout):
        for img in value or ():
            source = f"{spec.id}.{img['source']}"
            if wanted is None or source in wanted:
                out.setdefault(source, []).append({k: v for k, v in img.items() if k != "source"})
    return out


def season_hints(timeout=TIMEOUT):
    """``[{"app", "season", "reason"[, "opens_on"]}]`` from season providers that currently give a hint.

    ``opens_on`` (ISO date) means the season is scheduled to start that day rather than already started.
    """
    return [{"app": app_label(spec), **value}
            for spec, value in _run(SEASON, providers(SEASON), lambda s: _season_value, timeout)
            if value]
