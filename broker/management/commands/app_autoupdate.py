"""Update container apps to their newest catalog version.

Run every half hour by the container entrypoint with ``--nightly``, which only
acts between 03:00 and 03:59 hub time and only for apps with auto-update on.
An update that asks for new permissions is left for an admin to approve.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand

from broker import catalog, services, supervisor

NIGHT_HOUR = 3


def local_hour():
    """The hour where the hub is (its location's time zone), else UTC."""
    try:
        from bdh_sdk.location import location
        zone = ZoneInfo(location().timezone)
    except Exception:
        zone = ZoneInfo("UTC")
    return datetime.now(zone).hour


class Command(BaseCommand):
    help = "Update container apps that have a newer catalog version."

    def add_arguments(self, parser):
        parser.add_argument("--nightly", action="store_true",
                            help="Only run during the 03:00 hour, and only for auto-update apps.")
        parser.add_argument("--all", action="store_true",
                            help="Include apps with auto-update off.")
        parser.add_argument("--timeout", type=float, default=services.INSTALL_TIMEOUT)

    def handle(self, *args, nightly, all, timeout, **opts):
        if nightly and local_hour() != NIGHT_HOUR:
            return
        if not supervisor.enabled():
            if not nightly:
                self.stdout.write("supervisor not configured; nothing to do")
            return
        try:
            # Always revalidate (a cheap ETag 304 when unchanged): a cached copy
            # that is still "fresh" can miss a release published minutes ago.
            catalog.refresh(force=True)
        except Exception as exc:  # a stale catalog is still worth checking
            self.stderr.write(f"catalog refresh failed: {exc}")
        results = services.update_all(only_auto=nightly or not all, timeout=timeout)
        for app_id, outcome in results.items():
            self.stdout.write(f"{app_id}: {outcome}")
        if not results and not nightly:
            self.stdout.write("everything is up to date")
