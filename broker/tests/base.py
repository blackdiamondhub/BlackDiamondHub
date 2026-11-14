"""Shared fixtures for the broker tests."""
import copy

from broker import events_bridge, remote, visitor
from broker.models import AppInstall
from broker.ratelimit import limiter
from broker.tokens import token_for

MANIFEST = {
    "id": "cams",
    "name": "Cameras",
    "version": "1.2.0",
    "tiles": [
        {"title": "Cameras", "url": "/cameras/", "url_name": "cameras:index", "icon": "cams/icon.png",
         "section": "lodge", "subtitle": "Live", "order": 5, "modes": ["owner", "guest", "dashboard"]},
        {"title": "Not a tile", "url": "//evil.example/", "url_name": "x", "icon": "", "section": "lodge"},
    ],
    "hooks": ["status", "shortcuts", "run_shortcut", "run_action"],
    "settings": [
        {"key": "grid", "label": "Grid size", "type": "int", "default": 2, "help": "", "minimum": 1,
         "maximum": 4, "unit": "", "choices": None, "dynamic_choices": False},
        {"key": "speaker", "label": "Speaker", "type": "choice", "default": "", "help": "",
         "minimum": None, "maximum": None, "unit": "", "choices": None, "dynamic_choices": True},
    ],
    "settings_pages": [{"title": "Camera setup", "url": "/cameras/setup/", "subtitle": "", "icon": "fas fa-video"}],
    "stat_keys": [],
    "provides": [],
    "permissions": ["ha.read:sensor.*", "ha.call:light.turn_on", "alerts", "events.subscribe:ha/state/sensor/#"],
}


def manifest(**changes):
    out = copy.deepcopy(MANIFEST)
    out.update(changes)
    return out


def make_install(app_id="cams", grants=None, requested=None, upstream="http://127.0.0.1:9201", **kw):
    m = kw.pop("manifest", None) or manifest(id=app_id)
    requested = list(m["permissions"]) if requested is None else requested
    return AppInstall.objects.create(
        app_id=app_id, upstream=upstream, manifest=m, requested_permissions=requested,
        granted_permissions=list(requested if grants is None else grants), **kw)


def auth(install):
    return f"Bearer {token_for(install)}"


def reset_broker():
    remote.installs_changed()
    events_bridge.reset()
    limiter.reset()
    visitor.clear()
