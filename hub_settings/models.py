from django.conf import settings
from django.db import models


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
