"""Fetch container apps' manifests (all enabled apps, or the ones named)."""
from django.core.management.base import BaseCommand, CommandError

from broker import remote
from broker.models import AppInstall


class Command(BaseCommand):
    help = "Fetch and cache the manifests of container apps."

    def add_arguments(self, parser):
        parser.add_argument("app_ids", nargs="*")

    def handle(self, *args, app_ids, **opts):
        installs = AppInstall.objects.filter(enabled=True)
        if app_ids:
            installs = AppInstall.objects.filter(app_id__in=app_ids)
            missing = set(app_ids) - set(installs.values_list("app_id", flat=True))
            if missing:
                raise CommandError(f"no such app: {', '.join(sorted(missing))}")
        failed = 0
        for install in installs:
            try:
                manifest = remote.fetch_manifest(install)
            except remote.RemoteError as exc:
                failed += 1
                self.stderr.write(f"{install.app_id}: {exc}")
                continue
            self.stdout.write(f"{install.app_id}: {manifest.get('version', '?')}, "
                              f"{len(manifest.get('tiles') or ())} tile(s)")
        remote.installs_changed()
        if failed:
            raise CommandError(f"{failed} app(s) failed")
