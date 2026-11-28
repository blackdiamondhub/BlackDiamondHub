"""Turn UniFi Connect screens off overnight and back on in the morning.

The screensaver goes black at night on any screen. A UniFi Connect display can
also switch its backlight off: when its saver shows during the night the page
asks for that (:func:`sleep`), and :func:`wake_due` turns it back on once the
night is over. The page then closes its saver behind the dark screen, since the
tap that wakes the backlight never reaches the page. A tap wakes the screen
(the display reloads the page) and it sleeps again after the screensaver timeout.

Set up on the Display settings page: the console's address, a local user and
its password. Without them nothing here does anything.
"""
import logging
import threading
import warnings

import requests
from bdh_sdk import secrets
from bdh_sdk.storage import app_state
from urllib3.exceptions import InsecureRequestWarning

from .options import APP, value
from .schedule import is_night, lodge_minute, window

log = logging.getLogger(__name__)

SLEPT = "connect_slept"  # app_state key: IPs of the screens turned off tonight
TIMEOUT = 10
WAKE_EVERY = 60  # seconds


class ConnectError(Exception):
    pass


def config():
    """``(host, username, password)``, or None when not set up."""
    host = (value("connect_host") or "").strip().rstrip("/")
    username = (value("connect_username") or "").strip()
    password = secrets.get("connect_password", app=APP) if host and username else None
    if not (host and username and password):
        return None
    if "://" not in host:
        host = f"https://{host}"
    return host, username, password


def configured():
    try:
        return config() is not None
    except Exception:
        return False


def night_now():
    night = window()
    return bool(night) and is_night(lodge_minute(), night["from"], night["until"])


def send(ip, action):
    """Run ``action`` (e.g. ``display_off``) on the Connect screen at ``ip``.

    Returns False when Connect isn't set up or has no screen at ``ip``;
    raises :class:`ConnectError` when the console fails.
    """
    conf = config()
    if not conf:
        return False
    host, username, password = conf
    with requests.Session() as session:
        session.verify = False  # the console's own self-signed certificate
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                login = session.post(f"{host}/api/auth/login", timeout=TIMEOUT,
                                     json={"username": username, "password": password})
                login.raise_for_status()
                session.headers["X-Csrf-Token"] = login.headers.get("X-Csrf-Token", "")
                devices = session.get(f"{host}/proxy/connect/api/v2/devices", timeout=TIMEOUT)
                devices.raise_for_status()
                device = next((d for d in devices.json().get("data") or ()
                               if (d.get("extraInfo") or {}).get("ethIP") == ip), None)
                if device is None:
                    return False
                act = next((a for a in (device.get("type") or {}).get("supportedActions") or ()
                            if a.get("name") == action), None)
                if act is None:
                    raise ConnectError(f"{device.get('name') or ip} can't {action}")
                done = session.patch(f"{host}/proxy/connect/api/v2/devices/{device['id']}/status",
                                     timeout=TIMEOUT, json={"id": act["id"], "name": action})
                done.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            # Never the exception text: a failed login can echo what was sent.
            raise ConnectError(f"{action} on {ip} failed ({type(exc).__name__})") from None
    return True


def sleep(ip):
    """Turn the screen at ``ip`` off, if it's night and it's a Connect screen."""
    if not ip or not night_now() or not send(ip, "display_off"):
        return False
    state = app_state(APP)
    slept = state.get(SLEPT) or []
    if ip not in slept:
        state.set(SLEPT, [*slept, ip])
    log.info("Connect screen %s off for the night", ip)
    return True


def wake_due():
    """Turn the screens slept overnight back on once the night is over."""
    state = app_state(APP)
    slept = state.get(SLEPT) or []
    if not slept or night_now():
        return []
    woken, left = [], []
    for ip in slept:
        try:
            send(ip, "display_on")
            woken.append(ip)
        except ConnectError as exc:
            log.warning("%s", exc)
            left.append(ip)
    if left:
        state.set(SLEPT, left)
    else:
        state.delete(SLEPT)
    if woken:
        log.info("Connect screens on: %s", ", ".join(woken))
    return woken


def _wake_loop(stop):
    from django.db import close_old_connections

    while not stop.wait(WAKE_EVERY):
        try:
            wake_due()
        except Exception:
            log.exception("Connect wake check failed")
        finally:
            close_old_connections()


def start():
    stop = threading.Event()
    threading.Thread(target=_wake_loop, args=(stop,), name="display-connect-wake", daemon=True).start()
    return stop
