from django.conf import settings
from django.db import models
from django.utils import timezone


class AppSetting(models.Model):
    """A value saved on the Settings page for one app's declared Setting.

    It wins over the environment when the app reads the setting with
    ``bdh_sdk.settings.app_setting``. Deleting the row falls back to the
    environment or the declared default again.
    """

    app_id = models.CharField(max_length=64)
    key = models.CharField(max_length=64)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app_id", "key"], name="hub_settings_unique_app_key"),
        ]
        ordering = ["app_id", "key"]

    def __str__(self):
        return f"{self.app_id}.{self.key} = {self.value!r}"


class AppState(models.Model):
    """Something an app remembers between runs, via ``bdh_sdk.storage.app_state``."""

    app_id = models.CharField(max_length=64)
    key = models.CharField(max_length=128)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app_id", "key"], name="hub_settings_unique_state_key"),
        ]
        ordering = ["app_id", "key"]

    def __str__(self):
        return f"{self.app_id}.{self.key} = {self.value!r}"


class AppSecret(models.Model):
    """One app's secret, encrypted with AES-256-GCM (see :mod:`hub_settings.vault`).

    Only ever read through the vault: the key isn't in the database and the
    ciphertext is bound to ``app_id|name``.
    """

    app_id = models.CharField(max_length=64)
    name = models.CharField(max_length=64)
    key_id = models.CharField(max_length=32)
    nonce = models.BinaryField()
    ciphertext = models.BinaryField()
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app_id", "name"], name="hub_settings_unique_secret"),
        ]
        ordering = ["app_id", "name"]

    def __str__(self):
        return f"{self.app_id}.{self.name} (secret)"

    def __repr__(self):
        return f"<AppSecret {self.app_id}.{self.name}>"


class HAInstance(models.Model):
    """A Home Assistant instance apps can reach through ``bdh_sdk.ha``.

    The id is what apps pass as ``instance=`` and the prefix of the instance's
    event topics (``<id>/state/<entity_id>``), so it can't change once saved.
    The access token lives in the vault (``hub_settings.ha.token_name``).
    With no rows apps have no Home Assistant.
    """

    id = models.CharField(primary_key=True, max_length=40)
    name = models.CharField(max_length=80)
    site = models.CharField(max_length=80, blank=True)
    url = models.URLField(max_length=200)
    is_default = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Home Assistant instance"

    def __str__(self):
        return f"{self.name} ({self.id})"


class SpeechClip(models.Model):
    """A message spoken once by the hub's speech engine, kept so it's never made again.

    ``key`` is the sha256 of engine, voice and text (``hub_settings.speech.clip_key``).
    Clips unused for 90 days are deleted by ``manage.py speech_prune``.
    """

    SPEECH, ANNOUNCE, CUSTOM = "speech", "announce", "custom"
    KINDS = [(SPEECH, "Speech"), (ANNOUNCE, "Chime and speech"), (CUSTOM, "App clip")]

    key = models.CharField(primary_key=True, max_length=64)
    kind = models.CharField(max_length=16, choices=KINDS, default=SPEECH)
    engine = models.CharField(max_length=16)
    voice = models.CharField(max_length=80, blank=True)
    text = models.TextField()
    audio = models.BinaryField()
    content_type = models.CharField(max_length=40)
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-last_used"]

    def __str__(self):
        return f"{self.engine} {self.voice or '-'}: {self.text[:40]}"


class Speaker(models.Model):
    """A speaker the hub plays on (``hub_settings.speakers``).

    ``native_id`` is ``sonos:<RINCON uid>``, ``alexa:<HA instance>:<entity>``
    or ``ha:<HA instance>:<entity>``. ``legacy_id`` is the Home Assistant
    entity id apps saved before the hub had its own speakers (default
    instance only); it is kept when HA goes away so those ids keep working.
    ``ha_instance``/``ha_entity`` are the HA route to the speaker, used when
    the direct one fails. A speaker that disappears goes offline; rows are
    only deleted when they turn out not to be this site's (another
    household's Sonos, another HA instance's speakers) or not a room (a
    satellite, sub or stereo-pair secondary), or a Sonos this site's
    topology never had or hasn't had for a week. ``household`` is a Sonos
    room's household id; ``aliases`` are other ids that mean this room: the
    hidden members (``sonos:<uid>``) and their HA entities.
    """

    SONOS, ALEXA, HA = "sonos", "alexa", "ha"
    KINDS = [(SONOS, "Sonos"), (ALEXA, "Alexa"), (HA, "Home Assistant")]

    native_id = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=16, choices=KINDS)
    driver = models.CharField(max_length=16)
    name = models.CharField(max_length=200)
    legacy_id = models.CharField(max_length=200, null=True, blank=True, db_index=True)
    caps = models.JSONField(default=dict)
    online = models.BooleanField(default=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    address = models.CharField(max_length=64, blank=True)
    ha_instance = models.CharField(max_length=64, blank=True)
    ha_entity = models.CharField(max_length=200, blank=True)
    household = models.CharField(max_length=64, blank=True)
    aliases = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.native_id})"
