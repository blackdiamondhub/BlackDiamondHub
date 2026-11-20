"""Announce the hub on the LAN over mDNS/DNS-SD so displays can find it.

The BDH Display kiosk browses for ``_bdh-hub._tcp`` on first boot and lists
what it finds by name. Run by ``manage.py mdns_advertise`` (started from
``docker/entrypoint.sh``); the hub container uses host networking, so the
announcements reach the LAN directly. ``BDH_MDNS=off`` turns it off and
``BDH_MDNS_ADDRESS`` overrides the LAN address that is announced.
"""
import logging
import os
import socket

SERVICE_TYPE = "_bdh-hub._tcp.local."
INSTANCE_NAME = "Black Diamond Hub"

log = logging.getLogger(__name__)


def enabled():
    return os.getenv("BDH_MDNS", "on").strip().lower() not in ("off", "0", "false", "no")


def lan_address():
    """The IPv4 address this host uses to reach the LAN (the default route's source)."""
    override = os.getenv("BDH_MDNS_ADDRESS", "").strip()
    if override:
        return override
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1; nothing is sent
        return s.getsockname()[0]
    finally:
        s.close()


def service_properties(brand, site, port):
    """TXT record: what the kiosk shows and how it builds the hub URL."""
    props = {"path": "/", "port": str(port), "site": site or ""}
    if brand.get("name"):
        props["name"] = brand["name"]
    if brand.get("subtitle"):
        props["location"] = brand["subtitle"]
    return props


def service_info(address, port, brand, site, hostname=None):
    from zeroconf import ServiceInfo

    host = (hostname or socket.gethostname()).split(".")[0]
    return ServiceInfo(
        SERVICE_TYPE,
        f"{INSTANCE_NAME}.{SERVICE_TYPE}",
        port=port,
        properties=service_properties(brand, site, port),
        server=f"{host}.local.",
        parsed_addresses=[address],
    )


def run(port, brand, site, stop=None):
    """Register the service and block until ``stop`` (a threading.Event) is set."""
    import threading

    from zeroconf import IPVersion, Zeroconf

    stop = stop or threading.Event()
    address = lan_address()
    info = service_info(address, port, brand, site)
    zc = Zeroconf(interfaces=[address], ip_version=IPVersion.V4Only)
    try:
        # Another hub on the same LAN gets "Black Diamond Hub (2)".
        zc.register_service(info, allow_name_change=True)
        log.info("mDNS: announced %r at %s:%s", info.name, address, port)
        stop.wait()
    finally:
        zc.unregister_service(info)
        zc.close()
