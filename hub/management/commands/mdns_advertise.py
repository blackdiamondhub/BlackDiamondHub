import logging
import os
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand

from hub import mdns


class Command(BaseCommand):
    help = "Announce the hub on the LAN over mDNS (_bdh-hub._tcp) until stopped."

    def handle(self, *args, **options):
        if not mdns.enabled():
            self.stdout.write("mDNS off (BDH_MDNS)")
            return
        logging.basicConfig(level=logging.INFO, format="[mdns] %(message)s")
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        mdns.run(int(os.getenv("PORT", "8000")), settings.HUB_BRAND, settings.HUB_SITE, stop)
