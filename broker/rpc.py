"""The broker's ops: what a container app may ask the hub to do for it.

Each op takes the calling :class:`~broker.models.AppInstall`, its effective
grants and the JSON payload, checks the grants and calls the hub's own
in-process SDK functions. Ops raise :class:`OpError` to refuse.
"""
import base64
import binascii
from datetime import datetime

from bdh_sdk import events as bus
from django.utils.dateparse import parse_datetime

from . import grants as g

MAX_BODY = 256 * 1024
MAX_UPLOAD_BODY = 8 * 1024 * 1024
MAX_SUBSCRIPTIONS = 32

# Ops worth an audit entry even when allowed (they change something).
AUDITED = frozenset({
    "ha.call_service", "ha.upload_media", "ha.ws_commands", "speakers.announce",
    "alerts.push", "alerts.dismiss", "state.set", "state.delete", "events.subscribe",
})


class OpError(Exception):
    STATUS = {"forbidden": 403, "invalid": 400, "unknown_op": 404, "not_found": 404,
              "rate_limited": 429, "too_large": 413, "ha_error": 500,
              "announce_error": 500, "error": 500}

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = self.STATUS.get(code, 500)


def forbidden(message):
    return OpError("forbidden", message)


def invalid(message):
    return OpError("invalid", message)


def _str(payload, key, required=True):
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise invalid(f"{key} must be a string")
    return value


def _own(install, payload):
    """The payload's ``app``, which must be the caller's own id."""
    app = payload.get("app")
    if app != install.app_id:
        raise forbidden(f"{install.app_id} may not touch {app!r}'s data")
    return app


def _require(grants, grant):
    if grant not in grants:
        raise forbidden(f"needs the {grant!r} permission")


def _ha_call(fn, *args):
    from bdh_sdk import ha

    try:
        return fn(*args)
    except ha.HAError as exc:
        raise OpError("ha_error", str(exc)) from None


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _when(value, key):
    if value is None:
        return None
    when = parse_datetime(value) if isinstance(value, str) else None
    if when is None or when.tzinfo is None:
        raise invalid(f"{key} must be an ISO time with a timezone")
    return when


def _jsonable_history(history):
    out = {}
    for eid, points in (history or {}).items():
        out[eid] = [[t.isoformat() if isinstance(t, datetime) else t, v] for t, v in points]
    return out


# --- settings / state ---------------------------------------------------------

#: Hub-wide settings every app may read (bdh_sdk.location / bdh_sdk.units).
PUBLIC_HUB_SETTINGS = frozenset({"location", "units"})


def settings_get(install, grants, payload):
    from hub_settings import store

    if payload.get("app") == "hub" and payload.get("key") in PUBLIC_HUB_SETTINGS:
        app = "hub"
    else:
        app = _own(install, payload)
    try:
        return {"found": True, "value": store.lookup(app, _str(payload, "key"))}
    except KeyError:
        return {"found": False, "value": None}


def _state():
    from hub_settings.state import DatabaseStateBackend

    return DatabaseStateBackend()


def state_get(install, grants, payload):
    app = _own(install, payload)
    try:
        return {"found": True, "value": _state().get(app, _str(payload, "key"))}
    except KeyError:
        return {"found": False, "value": None}


def state_set(install, grants, payload):
    app = _own(install, payload)
    _state().set(app, _str(payload, "key"), payload.get("value"))
    return None


def state_delete(install, grants, payload):
    app = _own(install, payload)
    _state().delete(app, _str(payload, "key"))
    return None


def state_keys(install, grants, payload):
    return list(_state().keys(_own(install, payload)))


# --- alerts -------------------------------------------------------------------

def alerts_push(install, grants, payload):
    from bdh_sdk import alerts

    from .remote import page_url, raw_url

    _require(grants, g.ALERTS)
    _own(install, payload)
    fields = {k: payload[k] for k in ("app", "key", "title", "embed_url", "body", "ttl", "wake",
                                      "sound", "speak", "resound") if k in payload}
    # The app's paths are on its own origin: show them through the hub.
    if isinstance(fields.get("embed_url"), str) and fields["embed_url"].startswith("/"):
        fields["embed_url"] = page_url(install.app_id, "/_bare" + fields["embed_url"]) or fields["embed_url"]
    sound = fields.get("sound")
    if isinstance(sound, str) and sound.startswith("/"):
        fields["sound"] = raw_url(install.app_id, sound) or sound
    try:
        alert = alerts.Alert(**fields)
    except (TypeError, ValueError) as exc:
        raise invalid(str(exc)) from None
    alerts.push(alert)
    return None


def alerts_dismiss(install, grants, payload):
    from bdh_sdk import alerts

    _require(grants, g.ALERTS)
    app = _own(install, payload)
    try:
        alerts.dismiss(app, _str(payload, "key"))
    except ValueError as exc:
        raise invalid(str(exc)) from None
    return None


# --- events -------------------------------------------------------------------

def events_publish(install, grants, payload):
    from . import events_bridge

    topic = _str(payload, "topic")
    if not g.can_publish(grants, install.app_id, topic):
        raise forbidden(f"may not publish {topic!r}")
    events_bridge.publish(install.app_id, topic, payload.get("data"))
    return None


def events_subscribe(install, grants, payload):
    from . import events_bridge

    patterns = payload.get("patterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise invalid("patterns must be a list of strings")
    patterns = sorted(set(patterns))
    if len(patterns) > MAX_SUBSCRIPTIONS:
        raise invalid(f"at most {MAX_SUBSCRIPTIONS} patterns")
    for pattern in patterns:
        try:
            bus._segments(pattern, wildcards=True)
        except ValueError as exc:
            raise invalid(str(exc)) from None
    refused = [p for p in patterns if not g.can_subscribe(grants, install.app_id, p)]
    if refused:
        raise forbidden(f"may not subscribe to {', '.join(refused)}")
    events_bridge.set_patterns(install.app_id, patterns)
    return None


# --- Home Assistant -----------------------------------------------------------

def ha_get_entity_state(install, grants, payload):
    from bdh_sdk import ha

    eid = _str(payload, "entity_id")
    if not g.can_read(grants, eid):
        raise forbidden(f"may not read {eid}")
    return _ha_call(ha.get_entity_state, eid)


def _entity_ids(payload):
    ids = payload.get("entity_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise invalid("entity_ids must be a list of strings")
    return ids


def _readable(grants, ids):
    refused = [i for i in ids if not g.can_read(grants, i)]
    if refused:
        raise forbidden(f"may not read {', '.join(refused[:5])}")
    return ids


def ha_get_entity_states(install, grants, payload):
    from bdh_sdk import ha

    ids = _readable(grants, _entity_ids(payload))
    return _ha_call(ha.get_entity_states, ids) or {}


def ha_get_history(install, grants, payload):
    from bdh_sdk import ha

    ids = _readable(grants, _entity_ids(payload))
    start = _when(payload.get("start"), "start")
    if start is None:
        raise invalid("start is required")
    end = _when(payload.get("end"), "end")
    return _jsonable_history(_ha_call(ha.get_history, ids, start, end))


def ha_call_service(install, grants, payload):
    from bdh_sdk import ha

    domain, service = _str(payload, "domain"), _str(payload, "service")
    if not g.can_call(grants, domain, service):
        raise forbidden(f"may not call {domain}.{service}")
    extra = payload.get("extra_data")
    if extra is not None and not isinstance(extra, dict):
        raise invalid("extra_data must be an object")
    entity_id = payload.get("entity_id")
    if entity_id is not None and not isinstance(entity_id, (str, list)):
        raise invalid("entity_id must be a string or a list")
    ok, err = _ha_call(ha.call_service, domain, service, entity_id, extra)
    return [bool(ok), err]


def ha_upload_media(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.MEDIA)
    try:
        data = base64.b64decode(_str(payload, "data").encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        raise invalid("data must be base64") from None
    folder, filename = _str(payload, "folder"), _str(payload, "filename")
    if ".." in folder.split("/") or "/" in filename or filename in (".", ".."):
        raise invalid("bad folder or filename")
    return _ha_call(ha.upload_media, folder, filename, data, _str(payload, "content_type"))


def ha_get_tts(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.MEDIA)
    options = payload.get("options")
    if options is not None and not isinstance(options, dict):
        raise invalid("options must be an object")
    audio, ctype = _ha_call(ha.get_tts, _str(payload, "engine_id"), _str(payload, "message"), options)
    return {"audio": _b64(audio), "content_type": ctype}


def ha_get_all_states(install, grants, payload):
    from bdh_sdk import ha

    if not g.can_read_any(grants):
        raise forbidden("needs an ha.read permission")
    states = _ha_call(ha.get_all_states) or []
    return [s for s in states if isinstance(s, dict) and g.can_read(grants, s.get("entity_id"))]


def ha_get_static(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.MEDIA)
    path = _str(payload, "path")
    if not path.startswith("/") or path.startswith("//") or "\\" in path or ":" in path.split("?")[0]:
        raise invalid("path must be a path on Home Assistant")
    return _b64(_ha_call(ha.get_static, path))


def ha_ws_commands(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.HA_WS)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise invalid("messages must be a list of objects")
    return _ha_call(ha.ws_commands, *messages)


# --- speakers -----------------------------------------------------------------

def _speakers_call(fn, *args, **kwargs):
    from bdh_sdk import ha, speakers

    try:
        return fn(*args, **kwargs)
    except speakers.AnnounceError as exc:
        raise OpError("announce_error", str(exc)) from None
    except ha.HAError as exc:
        raise OpError("ha_error", str(exc)) from None
    except ValueError as exc:
        raise invalid(str(exc)) from None


def speakers_list(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    return [list(pair) for pair in _speakers_call(speakers.list_speakers) or ()]


def _tts_kwargs(payload):
    kwargs = {"voice": _str(payload, "voice", required=False)}
    if payload.get("tts_engine") is not None:
        kwargs["tts_engine"] = _str(payload, "tts_engine")
    return kwargs


def speakers_announce(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    ids = payload.get("speakers")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise invalid("speakers must be a list of strings")
    volume = payload.get("volume")
    if volume is not None and (isinstance(volume, bool) or not isinstance(volume, (int, float))):
        raise invalid("volume must be a number")
    failed = _speakers_call(speakers.announce, _str(payload, "message"), ids, volume,
                            _str(payload, "chime", required=False), **_tts_kwargs(payload))
    return list(failed or ())


def speakers_tts(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    audio, ctype = _speakers_call(speakers.tts_audio, _str(payload, "message"), **_tts_kwargs(payload))
    return {"audio": _b64(audio), "content_type": ctype}


# --- hub ------------------------------------------------------------------------

def hub_brand(install, grants, payload):
    from bdh_sdk import branding

    return branding.brand()


OPS = {
    "settings.get": settings_get,
    "state.get": state_get,
    "state.set": state_set,
    "state.delete": state_delete,
    "state.keys": state_keys,
    "alerts.push": alerts_push,
    "alerts.dismiss": alerts_dismiss,
    "events.publish": events_publish,
    "events.subscribe": events_subscribe,
    "ha.get_entity_state": ha_get_entity_state,
    "ha.get_entity_states": ha_get_entity_states,
    "ha.get_history": ha_get_history,
    "ha.call_service": ha_call_service,
    "ha.upload_media": ha_upload_media,
    "ha.get_tts": ha_get_tts,
    "ha.get_all_states": ha_get_all_states,
    "ha.get_static": ha_get_static,
    "ha.ws_commands": ha_ws_commands,
    "speakers.list": speakers_list,
    "speakers.announce": speakers_announce,
    "speakers.tts": speakers_tts,
    "hub.brand": hub_brand,
}


def max_body(op):
    return MAX_UPLOAD_BODY if op == "ha.upload_media" else MAX_BODY
