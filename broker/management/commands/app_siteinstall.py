"""Install the container apps ``site.toml`` turns on that aren't installed yet.

Run once by the container entrypoint shortly after start. Uninstall an app
for good by removing it from (or setting ``runtime = "inprocess"`` in)
site.toml; otherwise it comes back at the next start.
"""
from django.core.management.base import BaseCommand

from broker import catalog, services, supervisor


class Command(BaseCommand):
    help = "Install container apps enabled in site.toml that aren't installed."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=services.INSTALL_TIMEOUT)

    def handle(self, *args, timeout, **opts):
        if not supervisor.enabled():
            self.stdout.write("supervisor not configured; nothing to do")
            return
        try:
            catalog.refresh(force=True)
        except Exception as exc:  # a cached catalog may still have the app
            self.stderr.write(f"catalog refresh failed: {exc}")
        results = services.install_site_apps(timeout=timeout)
        for app_id, outcome in results.items():
            self.stdout.write(f"{app_id}: {outcome}")
        if not results:
            self.stdout.write("every site app is installed")