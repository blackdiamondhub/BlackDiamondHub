"""Write a new AES-256 key for the secret store (``hub_settings.vault``).

Without ``--append`` it makes the key file and refuses to touch an existing
one. With ``--append`` it adds a key to an existing file; that key becomes the
current one, so run ``rotate_secrets_key`` afterwards. The file is mode 0600
and the key is never printed.
"""
import os
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from hub_settings import vault


class Command(BaseCommand):
    help = "Create the secret store's key file (or --append a new key to rotate to)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=None,
                            help="Key file (default: settings.BDH_SECRETS_KEY_FILE).")
        parser.add_argument("--key-id", default=None,
                            help="Id for the new key (default: k<YYYYMMDD>).")
        parser.add_argument("--append", action="store_true",
                            help="Add a key to an existing file instead of creating one.")

    def handle(self, *args, path=None, key_id=None, append=False, **options):
        path = path or settings.BDH_SECRETS_KEY_FILE
        kid = key_id or f"k{date.today():%Y%m%d}"
        if not vault.KID.match(kid):
            raise CommandError("--key-id must be 1-32 of A-Z a-z 0-9 _ -")
        line = vault.key_line(kid, vault.new_key())
        if append:
            self._append(path, kid, line)
            self.stdout.write(f"Added key {kid!r} to {path}; it is now current. "
                              "Run rotate_secrets_key to re-encrypt existing secrets.")
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CommandError(f"{path} already exists; not overwriting it "
                               "(use --append to add a key for rotation).") from None
        except OSError as exc:
            raise CommandError(f"can't create {path}: {exc.strerror}") from None
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write("# BlackDiamondHub secret store keys: <key id>:<base64 key>. Last = current.\n")
            fh.write(line)
        os.chmod(path, 0o600)
        self.stdout.write(f"Wrote key {kid!r} to {path} (mode 0600). Back this file up somewhere safe: "
                          "without it the stored secrets can't be read.")

    def _append(self, path, kid, line):
        try:
            with open(path, encoding="ascii") as fh:
                text = fh.read()
            existing = vault.parse_keys(text)
        except FileNotFoundError:
            raise CommandError(f"{path} doesn't exist; run without --append to create it.") from None
        except vault.SecretStoreError as exc:
            raise CommandError(str(exc)) from None
        if kid in dict(existing):
            raise CommandError(f"{path} already has a key {kid!r}; pass another --key-id.")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(fd, "a", encoding="ascii") as fh:
            fh.write(("" if not text or text.endswith("\n") else "\n") + line)
        os.chmod(path, 0o600)
