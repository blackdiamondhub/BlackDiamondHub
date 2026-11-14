"""Fetch the app catalog now (it is otherwise refreshed hourly when read)."""
from django.core.management.base import BaseCommand, CommandError

from broker import catalog


class Command(BaseCommand):
    help = "Refresh the cached app catalog and list what this hub can install."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true", help="Don't list the apps.")

    def handle(self, *args, quiet, **opts):
        try:
            outcome = catalog.refresh(force=True)
        except catalog.CatalogError as exc:
            raise CommandError(str(exc)) from exc
        data = catalog.get(refresh_stale=False)
        api = catalog.hub_api()
        containers = [a for a in data["apps"] if any(v.get("hub_api") == api for v in catalog.versions(a, data))]
        self.stdout.write(f"catalog {outcome}: {len(data['apps'])} app(s), {len(containers)} installable "
                          f"as container apps here")
        if quiet:
            return
        for app_id, entry in sorted(data["apps"].items()):
            versions = catalog.versions(app_id, data)
            if versions and versions[0].get("hub_api") == api:
                self.stdout.write(f"  {app_id} {versions[0]['version']}")
            elif versions:
                self.stdout.write(f"  {app_id} {versions[0]['version']} (hub API {versions[0].get('hub_api')}: "
                                  f"in-process only)")
            else:
                newest = next(iter(entry.get("versions") or ()), None)
                why = catalog.compatible(newest)[1] if newest else "no versions"
                self.stdout.write(f"  {app_id} (not installable: {why})")
