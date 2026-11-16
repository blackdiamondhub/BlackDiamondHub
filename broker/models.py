"""Apps that run in their own container, and what the broker let them do."""
from django.db import models, transaction

from . import conf

AUDIT_KEEP = 2000
AUDIT_TRIM_EVERY = 100


class AppInstall(models.Model):
    """One container app. Its token is derived from SECRET_KEY, ``app_id`` and
    ``generation``; bump the generation to rotate it.

    ``runtime`` says who runs it: ``container`` apps are installed from the
    catalog and run by bdh-supervisor (see broker/services.py), ``external``
    ones are wherever ``upstream`` points (``broker_app``)."""

    EXTERNAL = "external"
    CONTAINER = "container"
    RUNTIMES = [(EXTERNAL, "External"), (CONTAINER, "Container (bdh-supervisor)")]

    app_id = models.SlugField(max_length=64, unique=True)
    runtime = models.CharField(max_length=16, choices=RUNTIMES, default=EXTERNAL)
    version = models.CharField(max_length=64, blank=True)
    generation = models.PositiveIntegerField(default=1)
    port = models.PositiveIntegerField(unique=True, help_text="Public port the hub proxies to this app.")
    upstream = models.CharField(max_length=200, blank=True,
                                help_text='Where the container listens, e.g. "http://127.0.0.1:9201".')
    network = models.CharField(max_length=32, blank=True)
    lifecycle = models.CharField(max_length=32, blank=True)
    memory_mb = models.PositiveIntegerField(null=True, blank=True)
    requested_permissions = models.JSONField(default=list, blank=True)
    granted_permissions = models.JSONField(default=list, blank=True)
    # What the admin approved for a container app; an update asking for more
    # (permissions, network, companions) waits for a new approval.
    approved_network = models.CharField(max_length=16, blank=True)
    allow_lan = models.BooleanField(default=False)
    approved_companions = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    auto_update = models.BooleanField(default=True, help_text="Install new catalog versions overnight.")
    manifest = models.JSONField(null=True, blank=True)
    manifest_fetched_at = models.DateTimeField(null=True, blank=True)
    version_changed = models.DateTimeField(null=True, blank=True, help_text="When an update last changed the version.")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["port"]

    def __str__(self):
        return self.app_id

    @property
    def grants(self):
        """Effective grants: what the manifest asks for and an admin approved."""
        requested = set(self.requested_permissions or ())
        return frozenset(p for p in self.granted_permissions or () if p in requested)

    @property
    def manifest_current(self):
        """Whether ``manifest`` is this version's. An update keeps the old one (so
        the app's pages stay up) until the new version's arrives."""
        if self.manifest is None:
            return False
        got = self.manifest.get("version")
        return not self.version or not got or got == self.version

    @property
    def is_container(self):
        return self.runtime == self.CONTAINER

    @property
    def slot(self):
        """The supervisor's slot: the app's index among the app ports."""
        return self.port - conf.app_port_base()

    @property
    def cookie_prefix(self):
        return f"bdh_{self.app_id.replace('-', '_')}_"

    @property
    def cookie_names(self):
        """The app's own cookies (as the SDK runtime names them). Matched exactly:
        a prefix match would hand app ``a`` the cookies of app ``a_b``."""
        return frozenset({self.cookie_prefix + "session", self.cookie_prefix + "csrf"})

    @staticmethod
    def cookie_key(app_id):
        """Ids that differ only by ``-``/``_`` share cookie names, so only one may exist."""
        return app_id.replace("-", "_")

    @staticmethod
    def free_port():
        used = set(AppInstall.objects.values_list("port", flat=True))
        base = conf.app_port_base()
        for port in range(base, base + conf.app_port_count()):
            if port not in used:
                return port
        raise ValueError("no free app port; raise APP_PORTS")

    def save(self, *args, **kwargs):
        if not self.port:
            self.port = self.free_port()
        super().save(*args, **kwargs)


class AppSubscription(models.Model):
    """What an app last asked the hub to deliver to it, so a hub restart doesn't
    lose it. ``kind`` is ``events`` (``payload`` = the pattern list) for now;
    ``instance`` lets later kinds keep one set per Home Assistant instance."""

    EVENTS = "events"

    app_id = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=16)
    instance = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=list)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["app_id", "kind", "instance"],
                                               name="broker_appsubscription_unique")]

    def __str__(self):
        return f"{self.app_id} {self.kind}{f' {self.instance}' if self.instance else ''}"


class AuditEntry(models.Model):
    """What an app asked the broker for (denials and changes, not every read)."""

    app_id = models.CharField(max_length=64, db_index=True)
    op = models.CharField(max_length=64)
    allowed = models.BooleanField()
    detail = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"{self.app_id} {self.op} {'ok' if self.allowed else 'denied'}"

    @classmethod
    def record(cls, app_id, op, allowed, detail=""):
        entry = cls.objects.create(app_id=app_id, op=op, allowed=allowed, detail=str(detail)[:500])
        if entry.pk % AUDIT_TRIM_EVERY == 0:
            cls.trim()
        return entry

    @classmethod
    def trim(cls, keep=AUDIT_KEEP):
        with transaction.atomic():
            cutoff = list(cls.objects.order_by("-id").values_list("id", flat=True)[keep:keep + 1])
            if cutoff:
                cls.objects.filter(id__lte=cutoff[0]).delete()
