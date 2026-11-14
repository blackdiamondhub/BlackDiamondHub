"""What the hub and bdh-supervisor know about container apps."""
import json

from django.core.management.base import BaseCommand

from broker import supervisor
from broker.models import AppInstall


class Command(BaseCommand):
    help = "Show installed container apps and the supervisor's view of them."

    def add_arguments(self, parser):
        parser.add_argument("app_ids", nargs="*")
        parser.add_argument("--json", dest="as_json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, app_ids, as_json, **opts):
        installs = AppInstall.objects.all()
        if app_ids:
            installs = installs.filter(app_id__in=app_ids)
        views, health, error = {}, None, ""
        if supervisor.enabled():
            try:
                health = supervisor.healthz()
                views = {v["app_id"]: v for v in supervisor.list_apps()}
            except supervisor.SupervisorError as exc:
                error = str(exc)
        else:
            error = "not configured"
        rows = []
        for install in installs:
            view = views.pop(install.app_id, None)
            rows.append({
                "app_id": install.app_id, "runtime": install.runtime, "version": install.version,
                "port": install.port, "enabled": install.enabled, "upstream": install.upstream,
                "network": install.approved_network, "allow_lan": install.allow_lan,
                "manifest": install.manifest is not None,
                "supervisor": None if view is None else {
                    k: view.get(k) for k in ("status", "desired", "installed_version", "network",
                                             "lifecycle", "upstream", "error", "last_activity")},
            })
        orphans = sorted(views) if not app_ids else sorted(set(views) & set(app_ids))
        if as_json:
            self.stdout.write(json.dumps({"supervisor": health, "error": error, "apps": rows, "orphans": orphans},
                                         indent=2, default=str))
            return
        if health is not None:
            self.stdout.write(f"supervisor: ok (prepared={health.get('prepared')}, hub_api={health.get('hub_api')}, "
                              f"apps={health.get('apps')}{', error: ' + health['error'] if health.get('error') else ''})")
        else:
            self.stdout.write(f"supervisor: {error}")
        if not rows:
            self.stdout.write("no apps installed")
        for row in rows:
            sup = row["supervisor"]
            state = "-" if sup is None else f"{sup['status']}/{sup['desired']}"
            self.stdout.write(f"{row['app_id']:<20} {row['runtime']:<9} {row['version'] or '-':<10} "
                              f"port {row['port']}  {state:<20} {row['upstream'] or '-'}"
                              f"{'' if row['manifest'] else '  (no manifest)'}"
                              f"{'  disabled' if not row['enabled'] else ''}")
            if sup and sup.get("error"):
                self.stdout.write(f"  error: {sup['error']}")
        for app_id in orphans:
            self.stdout.write(f"{app_id:<20} known to the supervisor only (app_uninstall to remove it)")
