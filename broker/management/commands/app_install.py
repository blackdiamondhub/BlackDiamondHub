"""Install or update a container app from the catalog (run by bdh-supervisor)."""
from django.core.management.base import BaseCommand, CommandError

from broker import catalog, services, supervisor


class Command(BaseCommand):
    help = "Install (or update) a container app from the app catalog."

    def create_parser(self, prog_name, subcommand, **kwargs):
        # Our --version (the app's) replaces Django's.
        return super().create_parser(prog_name, subcommand, conflict_handler="resolve", **kwargs)

    def add_arguments(self, parser):
        parser.add_argument("app_id")
        parser.add_argument("--version", dest="app_version", help="Catalog version (default: newest usable).")
        parser.add_argument("--network", choices=services.NETWORKS,
                            help="Network to approve (default: what the version asks for).")
        lan = parser.add_mutually_exclusive_group()
        lan.add_argument("--allow-lan", dest="allow_lan", action="store_true", default=None,
                         help="Let a network=lan app reach the LAN.")
        lan.add_argument("--no-allow-lan", dest="allow_lan", action="store_false")
        parser.add_argument("--approve", action="store_true",
                            help="Approve an update that asks for more permissions, network or companions.")
        parser.add_argument("--no-wait", action="store_true", help="Don't wait for the install to finish.")
        parser.add_argument("--timeout", type=float, default=services.INSTALL_TIMEOUT)

    def handle(self, *args, app_id, app_version, network, allow_lan, approve, no_wait, timeout, **opts):
        try:
            record = services.install(app_id, app_version, network=network, allow_lan=allow_lan,
                                      approve=approve, wait=not no_wait, timeout=timeout)
        except services.ReapprovalRequired as exc:
            raise CommandError(f"{exc}\nRun again with --approve to grant it.") from exc
        except (services.ServiceError, catalog.CatalogError, supervisor.SupervisorError) as exc:
            raise CommandError(str(exc)) from exc
        state = "requested" if no_wait else ("ready" if record.manifest_current else "installed (manifest pending)")
        self.stdout.write(f"{record.app_id} {record.version}: {state} on port {record.port} "
                          f"(slot {record.slot}, network {record.approved_network or 'none'}"
                          f"{', LAN allowed' if record.allow_lan else ''})")
        if record.upstream:
            self.stdout.write(f"  upstream {record.upstream}")
