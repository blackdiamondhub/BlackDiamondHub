"""Add, change or remove a container app's install record and print its token."""
from django.core.management.base import BaseCommand, CommandError

from broker import remote
from broker.models import AppInstall
from broker.tokens import token_for


class Command(BaseCommand):
    help = "Add/update/remove a container app (AppInstall) and print its broker token."

    def add_arguments(self, parser):
        parser.add_argument("app_id")
        parser.add_argument("--upstream", help='Where the container listens, e.g. "http://127.0.0.1:9201".')
        parser.add_argument("--app-version", dest="app_version")
        parser.add_argument("--grant", action="append", default=None,
                            help="Grant a permission (repeatable); replaces the granted set.")
        parser.add_argument("--grant-requested", action="store_true",
                            help="Grant everything the manifest requests.")
        parser.add_argument("--request", action="append", default=None,
                            help="Set requested permissions by hand (repeatable), before a manifest exists.")
        toggle = parser.add_mutually_exclusive_group()
        toggle.add_argument("--enable", action="store_true")
        toggle.add_argument("--disable", action="store_true")
        parser.add_argument("--rotate", action="store_true", help="Bump the token generation.")
        parser.add_argument("--refresh", action="store_true", help="Fetch the manifest now.")
        parser.add_argument("--remove", action="store_true")

    def handle(self, *args, app_id, **opts):
        install = AppInstall.objects.filter(app_id=app_id).first()
        if opts["remove"]:
            if install is None:
                raise CommandError(f"no app {app_id}")
            install.delete()
            self.stdout.write(f"removed {app_id}")
            return
        if install is None:
            key = AppInstall.cookie_key(app_id)
            clash = next((i.app_id for i in AppInstall.objects.all()
                          if AppInstall.cookie_key(i.app_id) == key), None)
            if clash:
                raise CommandError(f"{app_id} clashes with installed app {clash}")
            install = AppInstall(app_id=app_id)
        if opts["upstream"] is not None:
            install.upstream = opts["upstream"]
        if opts["app_version"] is not None:
            install.version = opts["app_version"]
        if opts["request"] is not None:
            install.requested_permissions = sorted(set(opts["request"]))
        if opts["grant"] is not None:
            install.granted_permissions = sorted(set(opts["grant"]))
        if opts["grant_requested"]:
            install.granted_permissions = sorted(set(install.requested_permissions or ()))
        if opts["enable"]:
            install.enabled = True
        if opts["disable"]:
            install.enabled = False
        if opts["rotate"]:
            install.generation += 1
        try:
            install.full_clean(exclude=["port"])
        except Exception as exc:
            raise CommandError(str(exc))
        install.save()
        if opts["refresh"]:
            try:
                remote.fetch_manifest(install)
            except remote.RemoteError as exc:
                self.stderr.write(f"manifest: {exc}")
            remote.installs_changed()
        self.stdout.write(f"app_id={install.app_id}")
        self.stdout.write(f"port={install.port}")
        self.stdout.write(f"upstream={install.upstream}")
        self.stdout.write(f"enabled={install.enabled}")
        self.stdout.write(f"grants={','.join(sorted(install.grants))}")
        self.stdout.write(f"BDH_APP_TOKEN={token_for(install)}")
