"""Move plain-text secret settings (``vault.LEGACY_PLAIN``) into the secret store.

For each mapped ``AppSetting`` with a non-empty value: encrypt it into
``AppSecret`` (unless that app already has a secret by that name, which wins)
and delete the plain row. Running it again finds nothing left to move.
``--dry-run`` reports what would move and changes nothing. Values are never
printed.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from hub_settings import vault
from hub_settings.models import AppSetting


class Command(BaseCommand):
    help = "Move plain-text secret settings (passwords, API keys) into the encrypted secret store."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would move; change nothing.")
        parser.add_argument("--app", action="append", default=None,
                            help="Only this app id (repeatable). Default: every mapped app.")

    @sensitive_variables("value")
    def handle(self, *args, dry_run=False, app=None, **options):
        apps = app or sorted(vault.LEGACY_PLAIN)
        unknown = [a for a in apps if a not in vault.LEGACY_PLAIN]
        if unknown:
            raise CommandError(f"no mapping for {', '.join(unknown)}; mapped apps: "
                               f"{', '.join(sorted(vault.LEGACY_PLAIN))}")
        if not dry_run and not vault.available():
            raise CommandError("the secret store has no key; run generate_secrets_key first")
        rows = [r for r in AppSetting.objects.filter(app_id__in=apps).order_by("app_id", "key")
                if vault.is_legacy(r.app_id, r.key)]
        moved = kept = blank = 0
        with transaction.atomic():
            existing = {(a, n) for a in apps for n in vault.names(a)}
            for row in rows:
                value = row.value
                label = f"{row.app_id}.{row.key}"
                if not isinstance(value, str) or not value:
                    action, blank = "drop empty plain value", blank + 1
                elif (row.app_id, row.key) in existing:
                    action, kept = "drop plain value (a secret is already stored)", kept + 1
                else:
                    action, moved = "move to secret store", moved + 1
                    if not dry_run:
                        vault.set_secret(row.app_id, row.key, value, user=row.updated_by)
                if not dry_run:
                    row.delete()
                self.stdout.write(f"{'would ' if dry_run else ''}{action}: {label}")
        verb = "Would move" if dry_run else "Moved"
        self.stdout.write(f"{verb} {moved} setting(s); {kept} already stored, {blank} empty. "
                          f"{'Nothing changed (dry run).' if dry_run else 'Plain values removed.'}")
