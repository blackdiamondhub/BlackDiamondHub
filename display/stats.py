"""Numbers for the screensaver's info strip, from the apps that provide them (``hub.provides``).

Every source is optional: a stat whose data is missing is left out.
"""
import logging

from .season import by_date, next_start

log = logging.getLogger(__name__)


def days_until_lifts(today, starts, opened, opens=None):
    """Days until the lifts open, while an app says they're still closed.

    Counts to ``opens`` (an opening day an app announced) when given, else to
    the winter start setting.
    """
    if opened is None or opened:
        return None
    if opens:
        days = (opens - today).days
    elif by_date(today, starts) == "winter":
        return None
    else:
        days = (next_start(today, starts["winter"]) - today).days
    return {"label": "Until lifts open", "value": "1 day" if days == 1 else f"{days} days"}


def collect(keys, today, starts, opened=None, opens=None):
    """The info strip for stat ids ``keys`` (see ``display.catalog``), in order, skipping gaps.

    ``opened`` is whether an app says winter has come (None: no app knows);
    ``opens`` is the opening day an app announced, if any.
    """
    from hub import provides

    from .catalog import owner

    owners = provides.stat_owners()
    wanted = {owner(k) for k in keys if owner(k) in owners}
    provided = provides.stats(wanted) if wanted else {}
    out = []
    for key in keys:
        try:
            if owner(key) in owners:
                item = provided.get(key)
                item = item and {k: v for k, v in item.items() if k != "order"}
            elif key == "days_until_lifts":
                item = days_until_lifts(today, starts, opened, opens)
            else:
                item = None
        except Exception as exc:
            log.warning("display stat %s failed: %s", key, exc)
            item = None
        if item:
            out.append(item)
    return out