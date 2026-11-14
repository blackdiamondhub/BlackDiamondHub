"""Ports and limits for the broker, the app proxy and the hub."""
from django.conf import settings


def hub_port():
    return int(getattr(settings, "HUB_PORT", 8000))


def broker_port():
    return int(getattr(settings, "BROKER_PORT", 8099))


def app_port_base():
    return int(getattr(settings, "APP_PORT_BASE", 9100))


def app_port_count():
    return int(getattr(settings, "APP_PORTS", 24))


def is_app_port(port):
    base = app_port_base()
    return port is not None and base <= int(port) < base + app_port_count()
