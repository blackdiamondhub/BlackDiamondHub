"""The hub's secret store: per-app secrets, encrypted with AES-256-GCM.

Values live in :class:`~hub_settings.models.AppSecret` rows as ciphertext.
The key never touches the database: a :class:`KeyProvider` supplies it, by
default :class:`FileKeyProvider` reading ``settings.BDH_SECRETS_KEY_FILE``
(``/etc/bdh/secrets.key``, bind-mounted read-only into the container).

The key file holds one key per line, ``<key id>:<base64 of 32 bytes>``; ``#``
starts a comment. The last key is the current one (new writes use it); the
others are only for reading rows not yet re-encrypted. ``manage.py
generate_secrets_key`` makes or appends a key and ``rotate_secrets_key``
re-encrypts every row under the current one.

Each ciphertext is bound to its row with the associated data ``app_id|name``,
so moving it to another app or name makes decryption fail.
"""
import base64
import binascii
import fnmatch
import os
import re
import secrets as _random
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.views.decorators.debug import sensitive_variables

KEY_BYTES = 32
NONCE_BYTES = 12
KID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class SecretStoreError(Exception):
    """The store can't be used (no key) or a row can't be decrypted. Never holds a value."""


# Settings that used to be plain AppSetting values and now live here
# (``manage.py migrate_plain_secrets`` moves them). Until each app declares them
# ``type=SECRET`` and reads ``bdh_sdk.secrets.get``, the Settings page treats them
# as secrets and ``app_setting`` still finds them (hub_settings.store), so moving
# them doesn't break an app that hasn't been updated. Usernames stay plain:
# alone they don't unlock anything and the admin needs to see them.
LEGACY_PLAIN = {
    "cameras": ("protect_api_key_*", "protect_password_*"),
    "device_control": ("gemstone_password",),
    "restaurants": ("google_places_api_key",),
    "shuttle": ("reminders_api_key",),
    "vacation_mode": ("modes_api_key",),
}


def is_legacy(app_id, key):
    return any(fnmatch.fnmatchcase(key, pattern) for pattern in LEGACY_PLAIN.get(app_id, ()))


# --- keys ------------------------------------------------------------------------

class KeyProvider:
    """Where the keys come from. Swap in vTPM, Proxmox or unlock-at-boot later."""

    def keys(self):
        """``{key_id: 32-byte key}`` for every key that may still be in use."""
        raise NotImplementedError

    def current(self):
        """``(key_id, key)`` that new ciphertexts use."""
        raise NotImplementedError


def new_key():
    return _random.token_bytes(KEY_BYTES)


@sensitive_variables()
def key_line(kid, key):
    if not KID.match(kid):
        raise ValueError("key id must be 1-32 of A-Z a-z 0-9 _ -")
    return f"{kid}:{base64.b64encode(key).decode('ascii')}\n"


@sensitive_variables()
def parse_keys(text):
    """``[(kid, key), ...]`` from a key file's text, in file order."""
    out, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kid, sep, b64 = line.partition(":")
        kid = kid.strip()
        if not sep or not KID.match(kid):
            raise SecretStoreError(f"key file line {number}: expected <key id>:<base64 key>")
        try:
            key = base64.b64decode(b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise SecretStoreError(f"key file line {number}: key is not base64") from None
        if len(key) != KEY_BYTES:
            raise SecretStoreError(f"key file line {number}: key must be {KEY_BYTES} bytes")
        if kid in seen:
            raise SecretStoreError(f"key file line {number}: duplicate key id {kid!r}")
        seen.add(kid)
        out.append((kid, key))
    return out


class FileKeyProvider(KeyProvider):
    """Keys from a file on the host, re-read when it changes."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp = None
        self._keys = []

    def _load(self):
        try:
            st = os.stat(self.path)
        except OSError:
            raise SecretStoreError(f"no secrets key file at {self.path}") from None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self._lock:
            if stamp != self._stamp:
                try:
                    with open(self.path, encoding="ascii") as fh:
                        keys = parse_keys(fh.read())
                except (OSError, UnicodeDecodeError):
                    raise SecretStoreError(f"can't read the secrets key file at {self.path}") from None
                if not keys:
                    raise SecretStoreError(f"the secrets key file at {self.path} has no keys")
                self._keys, self._stamp = keys, stamp
            return self._keys

    def keys(self):
        return dict(self._load())

    def current(self):
        return self._load()[-1]


class StaticKeyProvider(KeyProvider):
    """Keys held in memory (tests)."""

    def __init__(self, *pairs):
        self._keys = list(pairs)

    def keys(self):
        return dict(self._keys)

    def current(self):
        if not self._keys:
            raise SecretStoreError("no secrets key")
        return self._keys[-1]


_provider = None


def provider():
    global _provider
    if _provider is None:
        from django.conf import settings

        _provider = FileKeyProvider(settings.BDH_SECRETS_KEY_FILE)
    return _provider


def set_provider(new):
    """Use ``new`` for keys; ``None`` goes back to the key file."""
    global _provider
    _provider = new


def available():
    try:
        provider().current()
    except SecretStoreError:
        return False
    return True


# --- crypto ------------------------------------------------------------------------

def _aad(app_id, name):
    return f"{app_id}|{name}".encode()


@sensitive_variables()
def encrypt(app_id, name, value, key=None):
    """``(key_id, nonce, ciphertext)`` for ``value``, bound to ``app_id|name``."""
    kid, key = key or provider().current()
    nonce = os.urandom(NONCE_BYTES)
    return kid, nonce, AESGCM(key).encrypt(nonce, value.encode("utf-8"), _aad(app_id, name))


@sensitive_variables()
def decrypt(app_id, name, kid, nonce, ciphertext, keys=None):
    key = (keys or provider().keys()).get(kid)
    if key is None:
        raise SecretStoreError(f"{app_id}.{name}: key {kid!r} is not in the key file")
    try:
        plain = AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), _aad(app_id, name))
    except InvalidTag:
        raise SecretStoreError(f"{app_id}.{name}: can't decrypt (wrong key or tampered row)") from None
    return plain.decode("utf-8")


# --- the store ------------------------------------------------------------------------

def _model():
    from .models import AppSecret

    return AppSecret


@sensitive_variables()
def get_secret(app_id, name):
    """The secret's text; ``KeyError`` when unset."""
    row = _model().objects.filter(app_id=app_id, name=name).first()
    if row is None:
        raise KeyError(name)
    return decrypt(app_id, name, row.key_id, row.nonce, row.ciphertext)


@sensitive_variables()
def set_secret(app_id, name, value, user=None):
    from bdh_sdk.secrets import check_name, check_value

    check_name(name)
    check_value(value)
    kid, nonce, ciphertext = encrypt(app_id, name, value)
    _model().objects.update_or_create(app_id=app_id, name=name, defaults={
        "key_id": kid, "nonce": nonce, "ciphertext": ciphertext, "updated_by": user,
    })


def delete_secret(app_id, name):
    return _model().objects.filter(app_id=app_id, name=name).delete()[0] > 0


def names(app_id):
    """Names of ``app_id``'s secrets that are set (no values)."""
    return set(_model().objects.filter(app_id=app_id).values_list("name", flat=True))


@sensitive_variables()
def rotate():
    """Re-encrypt every row under the current key: ``(rotated, already current)``."""
    from django.db import transaction

    kid, key = provider().current()
    keys = provider().keys()
    rotated = current = 0
    with transaction.atomic():
        for row in _model().objects.select_for_update().order_by("pk"):
            if row.key_id == kid:
                current += 1
                continue
            value = decrypt(row.app_id, row.name, row.key_id, row.nonce, row.ciphertext, keys)
            row.key_id, row.nonce, row.ciphertext = encrypt(row.app_id, row.name, value, (kid, key))
            row.save(update_fields=["key_id", "nonce", "ciphertext", "updated"])
            rotated += 1
    return rotated, current


class DatabaseSecretBackend:
    """``bdh_sdk.secrets`` backend for apps running inside the hub."""

    def get(self, app_id, name):
        return get_secret(app_id, name)

    def set(self, app_id, name, value):
        set_secret(app_id, name, value)

    def delete(self, app_id, name):
        delete_secret(app_id, name)
