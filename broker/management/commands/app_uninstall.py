"""Remove an app: its container (bdh-supervisor) and the hub's record of it."""
from django.core.management.base import BaseCommand, CommandError

from broker import services, supervisor


class Command(BaseCommand):
    help = "Uninstall an app (with --purge, delete its data too)."

    def add_arguments(self, parser):
        parser.add_argument("app_id")
        parser.add_argument("--purge", action="store_true", help="Delete the app's data volume as well.")

    def handle(self, *args, app_id, purge, **opts):
        try:
            result = services.uninstall(app_id, purge=purge)
        except (services.ServiceError, supervisor.SupervisorError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{app_id}: uninstalled{' and its data deleted' if result.get('purged_data') else ''}")
