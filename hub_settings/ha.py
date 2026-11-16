"""The Home Assistant instances ``bdh_sdk.ha`` talks to, from Settings → Home Assistant.

With no saved instances the hub keeps using the single ``ha`` instance from
``HOMEASSISTANT_URL`` / ``HOMEASSISTANT_ACCESS_TOKEN``. Once any instance is
saved, only saved instances exist. Tokens are vault secrets of the ``hub`` owner.
"""

import re
import threading

from django.views.decorators.debug import sensitive_variables

VAULT_OWNER = "hub"
DEFAULT_ID = "ha"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_lock = threading.Lock()
_cache = None
_tokens = {}


def token_name(instance_id):
    return f"ha_token_{instance_id}"


def clear(**_kwargs):
    global _cache
    with _lock:
        _cache = None
        _tokens.clear()


def env_connection():
    from bdh_sdk import ha

    return ha._EnvProvider().connection(None)


def _rows():
    global _cache
    with _lock:
        if _cache is None:
            from .models import HAInstance

            _cache = list(HAInstance.objects.order_by("order", "name"))
        return _cache


def instances():
    """``[{id, name, site, default}]`` with exactly one default."""
    rows = _rows()
    if not rows:
        return [{"id": DEFAULT_ID, "name": "Home Assistant", "site": "", "default": True}]
    default = next((r.id for r in rows if r.is_default), rows[0].id)
    return [{"id": r.id, "name": r.name, "site": r.site, "default": r.id == default} for r in rows]


def default_id():
    return next(i["id"] for i in instances() if i["default"])


def exists(instance_id):
    return any(i["id"] == instance_id for i in instances())


@sensitive_variables()
def connection(instance=None):
    """``(url, token)`` for ``instance`` (``None`` is the default)."""
    from bdh_sdk.ha import HAError

    rows = _rows()
    if not rows:
        if instance not in (None, DEFAULT_ID):
            raise HAError(f"unknown Home Assistant instance {instance!r}")
        return env_connection()
    if instance is None:
        instance = default_id()
    row = next((r for r in rows if r.id == instance), None)
    if row is None:
        raise HAError(f"unknown Home Assistant instance {instance!r}")
    return row.url, _token(row.id)


@sensitive_variables()
def _token(instance_id):
    with _lock:
        if instance_id in _tokens:
            return _tokens[instance_id]
    from . import vault

    try:
        token = vault.get_secret(VAULT_OWNER, token_name(instance_id))
    except KeyError:
        token = ""
    with _lock:
        _tokens[instance_id] = token
    return token


class HubProvider:
    """The ``bdh_sdk.ha`` provider (``ha.set_provider``)."""

    def instances(self):
        return instances()

    def connection(self, instance=None):
        return connection(instance)
