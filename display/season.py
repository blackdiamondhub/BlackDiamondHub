"""Which season the screensaver shows."""
from datetime import date, datetime, timedelta, timezone

from .options import SEASON_IDS

EARLY_WINDOW = timedelta(days=30)
# An app's announced opening day is trusted only this close to the winter start setting.
OPENS_WINDOW = timedelta(days=60)



def lodge_tz():
    """The hub's time zone (Settings > General), UTC if it has none."""
    from bdh_sdk.location import tzinfo

    return tzinfo()


def lodge_today(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(lodge_tz()).date()


def parse_mmdd(text):
    """``"11-21"`` -> ``(11, 21)``. Raises ``ValueError`` with a message fit for a form."""
    month, sep, day = str(text).strip().partition("-")
    try:
        if not sep or len(month) != 2 or len(day) != 2:
            raise ValueError
        date(2000, int(month), int(day))  # 2000 is a leap year, so 02-29 is allowed
    except ValueError:
        raise ValueError("Use MM-DD, e.g. 11-21.") from None
    return int(month), int(day)


def _in_year(year, mmdd):
    month, day = parse_mmdd(mmdd)
    try:
        return date(year, month, day)
    except ValueError:  # 02-29 outside a leap year
        return date(year, month, 28)


def last_start(today, mmdd):
    """The most recent date on or before ``today`` that falls on ``mmdd``."""
    start = _in_year(today.year, mmdd)
    return start if start <= today else _in_year(today.year - 1, mmdd)


def next_start(today, mmdd):
    """The first date after ``today`` that falls on ``mmdd``."""
    start = _in_year(today.year, mmdd)
    return start if start > today else _in_year(today.year + 1, mmdd)


def opens_on(hint):
    """A season hint's scheduled start (``opens_on``), or None."""
    try:
        return date.fromisoformat(hint.get("opens_on"))
    except (TypeError, ValueError):
        return None


def _plausible(day, starts):
    """Whether an announced opening day is near the winter start setting."""
    winter = starts["winter"]
    return min(day - last_start(day, winter), next_start(day, winter) - day) <= OPENS_WINDOW


def winter_signal(hints, today, starts):
    """Whether a hint says winter has come.

    A scheduled hint (``opens_on``) counts from that day, and only for a while:
    a stale or implausible date (say, last year's) says nothing.
    """
    for hint in hints:
        if hint.get("season") != "winter":
            continue
        if not hint.get("opens_on"):
            return True
        day = opens_on(hint)
        if day and _plausible(day, starts) and timedelta(0) <= today - day <= OPENS_WINDOW:
            return True
    return False


def opening_day(hints, today, starts):
    """The earliest plausible winter opening day an app announces that is still ahead.

    Dates far from the winter start setting are ignored, so a bad feed can't
    replace the setting with something silly.
    """
    days = [day for hint in hints if hint.get("season") == "winter"
            and (day := opens_on(hint)) and day > today and _plausible(day, starts)]
    return min(days, default=None)


def by_date(today, starts):
    return max(SEASON_IDS, key=lambda season: last_start(today, starts[season]))


def resolve(today, starts, lifts_open=None, use_lifts=True, state=None):
    """The season for ``today``.

    Seasons follow the start dates, except that winter begins early when the
    lifts open within 30 days of its start date. The opening is remembered in
    ``state`` so winter sticks even if the lift report later goes quiet.
    Winter is only ever brought forward, never delayed.
    """
    season = by_date(today, starts)
    if not use_lifts or season == "winter":
        return season
    winter = next_start(today, starts["winter"])
    if today < winter - EARLY_WINDOW:
        return season
    opened = state.get("lifts_opened") if state is not None else None
    if opened:
        try:
            if winter - EARLY_WINDOW <= date.fromisoformat(opened) <= today:
                return "winter"
        except (TypeError, ValueError):
            pass
    if lifts_open:
        if state is not None:
            state.set("lifts_opened", today.isoformat())
        return "winter"
    return season
