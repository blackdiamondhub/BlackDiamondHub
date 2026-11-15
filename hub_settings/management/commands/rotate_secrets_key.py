"""Re-encrypt every stored secret under the current key (the key file's last line).

Rotation: ``generate_secrets_key --append``, then this command, then (once it
reports nothing left on old keys) remove the old key lines from the file.
"""
from django.core.management.base import BaseCommand, CommandError

from hub_settings import vault


class Command(BaseCommand):
    help = "Re-encrypt all secrets in the secret store under the current key."

    def handle(self, *args, **options):
        try:
            kid, _key = vault.provider().current()
            rotated, current = vault.rotate()
        except vault.SecretStoreError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Re-encrypted {rotated} secret(s) under key {kid!r}; "
                          f"{current} already used it. Older keys can now be removed from the key file.")
