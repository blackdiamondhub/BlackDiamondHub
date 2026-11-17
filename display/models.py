import secrets
from datetime import timedelta

from bdh_sdk.storage import app_media
from django.db import models
from django.utils import timezone

UPLOAD_MINUTES = 10


def media():
    return app_media("display")


class SaverPhoto(models.Model):
    """A photo in the screensaver library. No seasons means every season."""

    image = models.ImageField(storage=media, upload_to="photos/")
    thumb = models.ImageField(storage=media, upload_to="thumbs/")
    name = models.CharField(max_length=120, blank=True)
    seasons = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    taken_at = models.DateTimeField(null=True, blank=True)
    link = models.ForeignKey("UploadLink", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="photos")

    class Meta:
        ordering = ["-uploaded_at", "-id"]

    def in_season(self, season):
        return not self.seasons or season in self.seasons

    def delete(self, *args, **kwargs):
        for field in (self.image, self.thumb):
            if field:
                field.storage.delete(field.name)
        return super().delete(*args, **kwargs)


def _key():
    return secrets.token_urlsafe(18)


def _expiry():
    return timezone.now() + timedelta(minutes=UPLOAD_MINUTES)


class UploadLink(models.Model):
    """A short-lived link a phone uses to add photos. The first phone to open it claims it."""

    key = models.CharField(max_length=32, unique=True, default=_key)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_expiry)
    claim = models.CharField(max_length=48, blank=True)

    def live(self):
        return timezone.now() < self.expires_at

    def seconds_left(self):
        return max(0, int((self.expires_at - timezone.now()).total_seconds()))


class Heartbeat(models.Model):
    """When a browser showing the hub last checked in, by client IP.

    Lets a watchdog tell a hung or blank screen (no check-ins) from a working one.
    """

    ip = models.GenericIPAddressField(unique=True)
    last_seen = models.DateTimeField()
    path = models.CharField(max_length=200, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)


class DisplayLoad(models.Model):
    """One hub page load on a screen, and how the page before it ended.

    ``prev_clean`` False means the previous page never fired ``pagehide``: the
    browser crashed, was killed, or the screen lost power.
    """

    ip = models.GenericIPAddressField(db_index=True)
    at = models.DateTimeField(db_index=True)
    path = models.CharField(max_length=200, blank=True)
    nav_type = models.CharField(max_length=20, blank=True)
    referrer = models.CharField(max_length=200, blank=True)
    user_agent = models.CharField(max_length=200, blank=True)
    heap_mb = models.IntegerField(null=True, blank=True)
    prev_path = models.CharField(max_length=200, blank=True)
    prev_clean = models.BooleanField(null=True)
    prev_lived_s = models.IntegerField(null=True, blank=True)
    prev_silent_s = models.IntegerField(null=True, blank=True)
    prev_heap_mb = models.IntegerField(null=True, blank=True)
    # The previous page's last telemetry (see DisplaySample), so a crash shows what led up to it.
    prev_stats = models.JSONField(null=True, blank=True)


class DisplaySample(models.Model):
    """Page health a screen reports every 30 s: main-thread lag, long tasks, JS heap,
    and whatever the app in the page's frame sends (e.g. the cameras' decoder stats)."""

    ip = models.GenericIPAddressField(db_index=True)
    at = models.DateTimeField(db_index=True)
    path = models.CharField(max_length=200, blank=True)
    data = models.JSONField(default=dict)
