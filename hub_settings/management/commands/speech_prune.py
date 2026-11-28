"""Delete spoken clips nobody has used for a while, and app clips after an hour (run daily by docker/entrypoint.sh)."""
from django.core.management.base import BaseCommand

from hub_settings import speakers, speech


class Command(BaseCommand):
    help = "Delete cached speech clips unused for --days (default 90)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=speech.PRUNE_DAYS)

    def handle(self, *args, days, **options):
        removed = speakers.prune_custom() + speech.prune(days)
        self.stdout.write(f"{removed} speech clip(s) removed")
