"""The broker's ops: what a container app may ask the hub to do for it.

Each op takes the calling :class:`~broker.models.AppInstall`, its effective
grants and the JSON payload, checks the grants and calls the hub's own
in-process SDK functions. Ops raise :class:`OpError` to refuse.
"""
import base64
import binascii
from datetime import datetime

from bdh_sdk import events as bus
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from . import grants as g

MAX_BODY = 256 * 1024
MAX_UPLOAD_BODY = 8 * 1024 * 1024
MAX_SUBSCRIPTIONS = 32

# Ops worth an audit entry even when allowed (they change something).
AUDITED = frozenset({
    "ha.call_service", "ha.upload_media", "ha.ws_commands", "speakers.announce", "speakers.play",
    "alerts.push", "alerts.dismiss", "state.set", "state.delete", "events.subscribe",
    "secrets.get", "secrets.set", "secrets.delete",
    "shares.missing", "shares.put_blob", "shares.publish", "shares.list", "shares.open",
    "tools.open", "tools.call",
})

# Ops whose audit entry records only the secret's name, never the payload.
SECRET_OPS = frozenset({"secrets.get", "secrets.set", "secrets.delete"})


class OpError(Exception):
    STATUS = {"forbidden": 403, "invalid": 400, "unknown_op": 404, "not_found": 404,
              "rate_limited": 429, "too_large": 413, "bad_session": 400, "ha_error": 500,
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


def _instance(payload):
    """The payload's Home Assistant ``instance`` id, or ``None`` for the default."""
    from hub_settings import ha as instances

    value = payload.get("instance")
    if value is None:
        return None
    if not isinstance(value, str) or not instances.exists(value):
        raise invalid(f"unknown Home Assistant instance {value!r}")
    return value


def _ha_call(fn, *args, instance=None):
    from bdh_sdk import ha

    try:
        return fn(*args, instance=instance)
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
    key = _str(payload, "key")
    try:
        return {"found": True, "value": store.lookup(app, key)}
    except KeyError:
        pass
    # Then the site's [apps.<app>] table, as in-process apps get it.
    site_apps = getattr(settings, "HUB_SITE_APPS", {}) or {}
    if key in site_apps.get(app, {}):
        return {"found": True, "value": site_apps[app][key]}
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


# --- secrets -------------------------------------------------------------------
# The app is always the caller's own (from its token); a payload "app" is ignored.
# Errors never include the value.

def _secret_name(payload):
    from bdh_sdk.secrets import check_name

    try:
        return check_name(payload.get("name"))
    except ValueError:
        raise invalid("name must be 1-64 of a-z 0-9 _") from None


def _vault(fn, *args):
    from hub_settings.vault import SecretStoreError

    try:
        return fn(*args)
    except SecretStoreError:
        raise OpError("error", "secret store unavailable") from None


@sensitive_variables()
def secrets_get(install, grants, payload):
    from hub_settings import vault

    name = _secret_name(payload)
    try:
        return {"found": True, "value": _vault(vault.get_secret, install.app_id, name)}
    except KeyError:
        return {"found": False, "value": None}


@sensitive_variables()
def secrets_set(install, grants, payload):
    from bdh_sdk.secrets import check_value
    from hub_settings import vault

    name = _secret_name(payload)
    value = payload.get("value")
    try:
        check_value(value)
    except (TypeError, ValueError) as exc:
        raise invalid(str(exc)) from None
    _vault(vault.set_secret, install.app_id, name, value)
    return None


def secrets_delete(install, grants, payload):
    from hub_settings import vault

    vault.delete_secret(install.app_id, _secret_name(payload))
    return None


# --- alerts -------------------------------------------------------------------

def alerts_push(install, grants, payload):
    from bdh_sdk import alerts

    from .remote import page_url, raw_url

    _require(grants, g.ALERTS)
    _own(install, payload)
    fields = {k: payload[k] for k in ("app", "key", "title", "embed_url", "body", "ttl", "wake",
                                      "sound", "speak", "resound", "admins_only") if k in payload}
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
    if topic.startswith("_"):
        raise forbidden(f"topics starting with _ are the hub's own: {topic!r}")
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
    # The hub's own topics (``_shares/...``) reach an app directly when it
    # should get them, so they need no grant and aren't bus subscriptions.
    patterns = [p for p in patterns if not p.startswith("_")]
    refused = [p for p in patterns if not g.can_subscribe(grants, install.app_id, p)]
    if refused:
        raise forbidden(f"may not subscribe to {', '.join(refused)}")
    events_bridge.subscribe(install.app_id, patterns)
    return None


# --- shares ---------------------------------------------------------------------

def _shares_call(fn, *args):
    from . import shares

    try:
        return fn(*args)
    except shares.ShareError as exc:
        raise OpError(exc.code, str(exc)) from None


def shares_missing(install, grants, payload):
    from . import shares

    return {"missing": _shares_call(shares.missing, install.app_id, payload.get("sha256"))}


def shares_put_blob(install, grants, payload):
    from . import shares

    mime = _str(payload, "mime")
    try:
        data = base64.b64decode(_str(payload, "data_b64").encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        raise invalid("data_b64 must be base64") from None
    return {"sha256": _shares_call(shares.put_blob, install.app_id, mime, data)}


def shares_publish(install, grants, payload):
    from . import shares

    revision = _shares_call(shares.publish, install, payload.get("name"), payload.get("main"),
                            payload.get("attachments"), payload.get("expected_revision"))
    return {"revision": revision}


def shares_list(install, grants, payload):
    from . import shares

    out = shares.list_for(install)
    for share in out:
        share["updated"] = share["updated"].isoformat() if share["updated"] else None
    return {"shares": out}


def shares_open(install, grants, payload):
    from . import shares

    return _shares_call(shares.open_share, install, payload.get("provider"), payload.get("name"),
                        payload.get("if_revision"))


# --- Home Assistant -----------------------------------------------------------

def ha_get_entity_state(install, grants, payload):
    from bdh_sdk import ha

    eid = _str(payload, "entity_id")
    if not g.can_read(grants, eid):
        raise forbidden(f"may not read {eid}")
    return _ha_call(ha.get_entity_state, eid, instance=_instance(payload))


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
    return _ha_call(ha.get_entity_states, ids, instance=_instance(payload)) or {}


def ha_get_history(install, grants, payload):
    from bdh_sdk import ha

    ids = _readable(grants, _entity_ids(payload))
    start = _when(payload.get("start"), "start")
    if start is None:
        raise invalid("start is required")
    end = _when(payload.get("end"), "end")
    return _jsonable_history(_ha_call(ha.get_history, ids, start, end, instance=_instance(payload)))


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
    ok, err = _ha_call(ha.call_service, domain, service, entity_id, extra, instance=_instance(payload))
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
    return _ha_call(ha.upload_media, folder, filename, data, _str(payload, "content_type"),
                    instance=_instance(payload))


def ha_get_tts(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.MEDIA)
    options = payload.get("options")
    if options is not None and not isinstance(options, dict):
        raise invalid("options must be an object")
    audio, ctype = _ha_call(ha.get_tts, _str(payload, "engine_id"), _str(payload, "message"), options,
                            instance=_instance(payload))
    return {"audio": _b64(audio), "content_type": ctype}


def ha_get_all_states(install, grants, payload):
    from bdh_sdk import ha

    if not g.can_read_any(grants):
        raise forbidden("needs an ha.read permission")
    states = _ha_call(ha.get_all_states, instance=_instance(payload)) or []
    return [s for s in states if isinstance(s, dict) and g.can_read(grants, s.get("entity_id"))]


def ha_registry(install, grants, payload):
    """The entity/device registries, limited to entities the app may read."""
    from bdh_sdk import ha

    if not g.can_read_any(grants):
        raise forbidden("needs an ha.read permission")
    platform = _str(payload, "platform", required=False)
    reg = _ha_call(lambda instance: ha.get_registry(platform=platform, instance=instance),
                   instance=_instance(payload)) or {}
    entities = [e for e in reg.get("entities") or () if isinstance(e, dict)
                and (platform is None or e.get("platform") == platform)
                and g.can_read(grants, e.get("entity_id"))]
    device_ids = {e.get("device_id") for e in entities if e.get("device_id")}
    devices = [d for d in reg.get("devices") or () if isinstance(d, dict) and d.get("id") in device_ids]
    area_ids = {x.get("area_id") for x in (*entities, *devices) if x.get("area_id")}
    areas = [a for a in reg.get("areas") or () if isinstance(a, dict) and a.get("area_id") in area_ids]
    return {"entities": entities, "devices": devices, "areas": areas,
            "unit_system": reg.get("unit_system") or {}}


def ha_instances(install, grants, payload):
    from hub_settings import ha as instances

    if not g.can_read_any(grants):
        raise forbidden("needs an ha.read permission")
    return instances.instances()


def ha_get_static(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.MEDIA)
    path = _str(payload, "path")
    if not path.startswith("/") or path.startswith("//") or "\\" in path or ":" in path.split("?")[0]:
        raise invalid("path must be a path on Home Assistant")
    return _b64(_ha_call(ha.get_static, path, instance=_instance(payload)))


def ha_ws_commands(install, grants, payload):
    from bdh_sdk import ha

    _require(grants, g.HA_WS)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise invalid("messages must be a list of objects")
    return _ha_call(ha.ws_commands, *messages, instance=_instance(payload))


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


def speakers_devices(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    return _speakers_call(speakers.devices) or []


def _tts_kwargs(payload):
    kwargs = {"voice": _str(payload, "voice", required=False)}
    if payload.get("tts_engine") is not None:
        kwargs["tts_engine"] = _str(payload, "tts_engine")
    return kwargs


def _speaker_ids(payload):
    ids = payload.get("speakers")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise invalid("speakers must be a list of strings")
    return ids


def _volume(payload):
    volume = payload.get("volume")
    if volume is not None and (isinstance(volume, bool) or not isinstance(volume, (int, float))):
        raise invalid("volume must be a number")
    return volume


def speakers_announce(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    ids, volume = _speaker_ids(payload), _volume(payload)
    detail = payload.get("detail") is True
    kwargs = _tts_kwargs(payload)
    if detail:
        kwargs["detail"] = True
    result = _speakers_call(speakers.announce, _str(payload, "message"), ids, volume,
                            _str(payload, "chime", required=False), **kwargs)
    if detail:
        return dict(result or {})
    return list(result or ())


def speakers_play(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    ids, volume = _speaker_ids(payload), _volume(payload)
    try:
        audio = base64.b64decode(_str(payload, "audio").encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        raise invalid("audio must be base64") from None
    if len(audio) > speakers.MAX_PLAY_BYTES:
        raise OpError("too_large", "audio must be at most 2 MB")
    detail = payload.get("detail") is True
    result = _speakers_call(speakers.play, audio, ids, volume, night=payload.get("night") is True, detail=detail)
    if detail:
        return dict(result or {})
    return list(result or ())


def speakers_tts(install, grants, payload):
    from bdh_sdk import speakers

    _require(grants, g.SPEAKERS)
    audio, ctype = _speakers_call(speakers.tts_audio, _str(payload, "message"), **_tts_kwargs(payload))
    return {"audio": _b64(audio), "content_type": ctype}


# --- hub ------------------------------------------------------------------------

def hub_brand(install, grants, payload):
    from bdh_sdk import branding

    return branding.brand()


def displays_status(install, grants, payload):
    """Last heartbeat per client IP. Only IPs and times: no paths or user agents."""
    from django.utils import timezone

    from display.apps import DisplayConfig
    from display.models import Heartbeat

    _require(grants, g.DISPLAYS_READ)
    now = timezone.now()
    ready = DisplayConfig.ready_since or now
    return {
        "ready_since": ready.isoformat(),
        "now": now.isoformat(),
        "clients": [
            {"ip": ip, "last_seen": seen.isoformat(),
             "age_seconds": max(0, int((now - seen).total_seconds()))}
            for ip, seen in Heartbeat.objects.order_by("ip").values_list("ip", "last_seen")
        ],
    }


def stays_current(install, grants, payload):
    """The stay in progress (cached, last-good, never past checkout), or None."""
    from hub import modes, status

    _require(grants, g.STAYS_READ)
    stay = status.current_stay()
    if not stay:
        return {"stay": None}

    def iso(t):
        return t.isoformat() if t else None
    return {"stay": {"guest": stay["guest"], "arrival": iso(stay["arrival"]),
                     "checkout": iso(stay["checkout"]), "owner": bool(stay["owner"]),
                     "key": modes.stay_key(stay)}}


def _session(install, payload):
    from hub import tools

    try:
        return tools.load_session(payload.get("token"), install.app_id)
    except tools.BadSession as exc:
        raise OpError(tools.BAD_SESSION, str(exc)) from None


def tools_open(install, grants, payload):
    """Start an assistant session; the hub picks its mode (see ``hub.tools``)."""
    from hub import tools

    _require(grants, g.TOOLS)
    kind = payload.get("kind", tools.VOICE)
    if kind not in tools.KINDS:
        raise invalid(f"unknown session kind {kind!r}")
    display = _str(payload, "display", required=False)
    return tools.open_session(install.app_id, kind, display)


def tools_list(install, grants, payload):
    from hub import tools

    _require(grants, g.TOOLS)
    session = _session(install, payload)
    return {"tools": tools.listing(tools.session_mode(session["kind"]))}


def tools_call(install, grants, payload):
    from hub import tools

    _require(grants, g.TOOLS)
    session = _session(install, payload)
    return tools.call(session, payload.get("name"), payload.get("args"))


OPS = {
    "settings.get": settings_get,
    "state.get": state_get,
    "state.set": state_set,
    "state.delete": state_delete,
    "state.keys": state_keys,
    "secrets.get": secrets_get,
    "secrets.set": secrets_set,
    "secrets.delete": secrets_delete,
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
    "ha.instances": ha_instances,
    "ha.registry": ha_registry,
    "speakers.list": speakers_list,
    "speakers.devices": speakers_devices,
    "speakers.announce": speakers_announce,
    "speakers.play": speakers_play,
    "speakers.tts": speakers_tts,
    "hub.brand": hub_brand,
    "displays.status": displays_status,
    "stays.current": stays_current,
    "shares.missing": shares_missing,
    "shares.put_blob": shares_put_blob,
    "shares.publish": shares_publish,
    "shares.list": shares_list,
    "shares.open": shares_open,
    "tools.open": tools_open,
    "tools.list": tools_list,
    "tools.call": tools_call,
}

UPLOAD_OPS = frozenset({"ha.upload_media", "shares.put_blob", "speakers.play"})


def max_body(op):
    return MAX_UPLOAD_BODY if op in UPLOAD_OPS else MAX_BODY
