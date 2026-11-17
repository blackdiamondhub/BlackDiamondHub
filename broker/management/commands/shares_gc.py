"""Drop old share revisions and the blobs nothing uses any more (see broker/shares.py)."""
from django.core.management.base import BaseCommand

from broker import shares


class Command(BaseCommand):
    help = "Collect garbage in app shares: old revisions and unused blobs."

    def handle(self, *args, **opts):
        revisions, blobs = shares.gc()
        self.stdout.write(f"deleted {revisions} revisions and {blobs} blobs")