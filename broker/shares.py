"""App shares: files a provider app publishes for the apps an admin granted.

A share is declared in the provider's manifest (``shares``) and published as
immutable revisions, each one main file plus image attachments, stored as
blobs in the database. Consumers read it with ``shares.open`` once the admin
has granted them (Settings → Apps → <provider> → Sharing). See the broker ops
in broker/rpc.py and the attachment view in broker/views.py.
"""
import base64
import hashlib
import io
import logging
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from . import catalog
from .models import AppInstall, ShareBlob, ShareGrant, ShareRevision

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MIME_RE = re.compile(r"[a-z]+/[a-z0-9.+-]+")
KEY_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
MAX_TITLE = 80

MAX_BLOB = 6 * 1024 * 1024
MAX_CALENDAR = 2 * 1024 * 1024
QUOTA = 50 * 1024 * 1024
MAX_MISSING = 500
MAX_ATTACHMENTS = 200
MAX_SIDE = 4096
MAX_PIXELS = 16 * 1024 * 1024
KEEP = 2
PENDING_FOR = timedelta(hours=1)

CALENDAR = "text/calendar"
IMAGES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
TYPES = frozenset({CALENDAR, *IMAGES})


class ShareError(Exception):
    """Carries an OpError code (``invalid``, ``too_large``, ...)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def attachment_path(sha):
    return f"/apps/_share/{sha}"


# --- manifests -------------------------------------------------------------------

def declared(manifest):
    """The valid ``shares`` in ``manifest`` (older SDKs send none); bad entries
    are skipped rather than breaking the app."""
    out, seen = [], set()
    entries = (manifest or {}).get("shares") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name, mime, title = entry.get("name"), entry.get("type"), entry.get("title")
        if (not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in seen
                or not isinstance(mime, str) or not MIME_RE.fullmatch(mime)
                or not isinstance(title, str) or not 1 <= len(title) <= MAX_TITLE):
            continue
        seen.add(name)
        out.append({"name": name, "type": mime, "title": title})
    return out


def reads(manifest):
    entries = (manifest or {}).get("reads") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        return set()
    return {m for m in entries if isinstance(m, str) and MIME_RE.fullmatch(m)}


def declaration(install, name):
    for share in declared(install.manifest if install else None):
        if share["name"] == name:
            return share
    return None


# --- blobs -------------------------------------------------------------------------

def _check_image(mime, data):
    """``(width, height)`` if ``data`` really is a ``mime`` image of a sane size."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = img.format, img.size
            if fmt != IMAGES[mime]:
                raise ShareError("invalid", f"data is not {mime}")
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise ShareError("invalid", f"image is {width}x{height}; at most {MAX_SIDE} a side "
                                            f"and {MAX_PIXELS} pixels")
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except ShareError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ShareError("invalid", f"not a valid {mime} image: {exc}") from None
    return width, height


def used(owner):
    return ShareBlob.objects.filter(owner=owner).aggregate(n=Sum("size"))["n"] or 0


def missing(owner, hashes):
    if (not isinstance(hashes, list) or len(hashes) > MAX_MISSING
            or not all(isinstance(h, str) and SHA_RE.fullmatch(h) for h in hashes)):
        raise ShareError("invalid", f"sha256 must be a list of at most {MAX_MISSING} hex digests")
    have = set(ShareBlob.objects.filter(owner=owner, sha256__in=hashes).values_list("sha256", flat=True))
    return [h for h in dict.fromkeys(hashes) if h not in have]


def put_blob(owner, mime, data):
    """Store ``data`` for ``owner``; returns its sha256."""
    if mime not in TYPES:
        raise ShareError("invalid", f"unsupported type {mime!r}")
    if len(data) > MAX_BLOB:
        raise ShareError("too_large", f"blobs are at most {MAX_BLOB} bytes")
    sha = hashlib.sha256(data).hexdigest()
    stored = ShareBlob.objects.filter(owner=owner, sha256=sha).values_list("mime", flat=True).first()
    if stored == mime:
        return sha
    if stored:
        raise ShareError("invalid", f"already stored as {stored}")
    width = height = None
    if mime == CALENDAR:
        if len(data) > MAX_CALENDAR:
            raise ShareError("too_large", f"text/calendar is at most {MAX_CALENDAR} bytes")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ShareError("invalid", "text/calendar must be UTF-8") from None
    else:
        width, height = _check_image(mime, data)
    if used(owner) + len(data) > QUOTA:
        gc()
        if used(owner) + len(data) > QUOTA:
            raise ShareError("too_large", f"shares of {owner} would exceed {QUOTA // (1024 * 1024)} MB")
    try:
        with transaction.atomic():
            ShareBlob.objects.create(sha256=sha, owner=owner, mime=mime, size=len(data), data=data,
                                     width=width, height=height)
    except IntegrityError:
        pass  # the same bytes arrived concurrently
    return sha


# --- revisions ------------------------------------------------------------------

def latest(provider, name):
    return ShareRevision.objects.filter(provider=provider, name=name).order_by("-revision").first()


def publish(install, name, main, attachments, expected_revision):
    """Publish a new revision of ``install``'s share ``name``; returns its number.
    Notifies the provider and granted consumers, then collects garbage, after commit."""
    from . import events_bridge

    if not isinstance(name, str):
        raise ShareError("invalid", "name must be a string")
    share = declaration(install, name)
    if share is None:
        raise ShareError("invalid", f"share {name!r} isn't declared in the app's manifest")
    if not isinstance(main, str) or not SHA_RE.fullmatch(main):
        raise ShareError("invalid", "main must be a sha256 hex digest")
    if attachments is None:
        attachments = {}
    if not isinstance(attachments, dict):
        raise ShareError("invalid", "attachments must be an object")
    if len(attachments) > MAX_ATTACHMENTS:
        raise ShareError("invalid", f"at most {MAX_ATTACHMENTS} attachments")
    for key, sha in attachments.items():
        if not KEY_RE.fullmatch(key):
            raise ShareError("invalid", f"bad attachment key {key!r}")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise ShareError("invalid", f"attachment {key!r} must be a sha256 hex digest")
    if expected_revision is not None and (isinstance(expected_revision, bool)
                                          or not isinstance(expected_revision, int)):
        raise ShareError("invalid", "expected_revision must be an integer or null")
    owner = install.app_id
    try:
        with transaction.atomic():
            wanted = {main, *attachments.values()}
            # Locked so a concurrent gc() can't delete them under this revision.
            blobs = {b.sha256: b for b in ShareBlob.objects.select_for_update()
                     .filter(owner=owner, sha256__in=wanted).only("id", "sha256", "mime")}
            blob = blobs.get(main)
            if blob is None or blob.mime != share["type"]:
                raise ShareError("invalid", f"main must be one of your own {share['type']} blobs")
            for key, sha in attachments.items():
                att = blobs.get(sha)
                if att is None or att.mime not in IMAGES:
                    raise ShareError("invalid", f"attachment {key!r} must be one of your own image blobs")
            current = latest(owner, name)
            number = current.revision if current else 0
            if expected_revision is not None and expected_revision != number:
                raise ShareError("invalid", f"conflict: the share is at revision {number}, "
                                            f"not {expected_revision}")
            ShareRevision.objects.create(provider=owner, name=name, revision=number + 1, main_sha=blob,
                                         attachments=dict(attachments))
    except IntegrityError:
        raise ShareError("invalid", "conflict: another publish of this share won") from None
    revision = number + 1

    def after():
        try:
            recipients = [owner, *ShareGrant.objects.filter(provider=owner, name=name)
                          .exclude(consumer=owner).values_list("consumer", flat=True)]
            events_bridge.notify_share(owner, name, revision, recipients)
        except Exception:
            log.exception("couldn't notify consumers of %s/%s", owner, name)
        try:
            gc()
        except Exception:
            log.exception("share gc failed")

    transaction.on_commit(after)
    return revision


def granted(consumer, provider, name):
    return ShareGrant.objects.filter(consumer=consumer, provider=provider, name=name).exists()


def _installs():
    return {i.app_id: i for i in AppInstall.objects.all()}


def list_for(install):
    """The caller's own shares plus the ones granted to it."""
    installs = _installs()
    pairs = {(install.app_id, s["name"]) for s in declared(install.manifest)}
    pairs |= set(ShareGrant.objects.filter(consumer=install.app_id).values_list("provider", "name"))
    out = []
    for provider, name in sorted(pairs):
        share = declaration(installs.get(provider), name)
        if share is None:
            continue
        rev = latest(provider, name)
        out.append({"provider": provider, "provider_title": catalog.display_name(installs[provider]),
                    "name": name, "title": share["title"], "type": share["type"],
                    "revision": rev.revision if rev else 0, "updated": rev.created if rev else None})
    return out


def open_share(install, provider, name, if_revision):
    if not isinstance(provider, str) or not isinstance(name, str):
        raise ShareError("invalid", "provider and name must be strings")
    if if_revision is not None and (isinstance(if_revision, bool) or not isinstance(if_revision, int)):
        raise ShareError("invalid", "if_revision must be an integer or null")
    if provider != install.app_id and not granted(install.app_id, provider, name):
        raise ShareError("forbidden", f"{provider}/{name} isn't shared with {install.app_id}")
    share = declaration(AppInstall.objects.filter(app_id=provider).first(), name)
    rev = latest(provider, name) if share else None
    if rev is None:
        raise ShareError("not_found", f"no share {provider}/{name}")
    if if_revision == rev.revision:
        return {"revision": rev.revision, "not_modified": True}
    return {"revision": rev.revision, "type": share["type"], "title": share["title"],
            "data_b64": base64.b64encode(bytes(rev.main_sha.data)).decode("ascii"),
            "attachments": {k: attachment_path(sha) for k, sha in (rev.attachments or {}).items()}}


def public_blob(sha):
    """The image blob ``sha`` if a kept revision has it as an attachment, else None."""
    if not SHA_RE.fullmatch(sha or ""):
        return None
    owners = set(ShareBlob.objects.filter(sha256=sha, mime__in=list(IMAGES)).values_list("owner", flat=True))
    if not owners:
        return None
    for rev in ShareRevision.objects.filter(provider__in=owners).only("provider", "attachments"):
        if sha in (rev.attachments or {}).values():
            return ShareBlob.objects.filter(sha256=sha, owner=rev.provider).first()
    return None


# --- garbage collection and cleanup ----------------------------------------------

def _referenced():
    """``(main blob ids, {(owner, sha)})`` that kept revisions use."""
    mains, atts = set(), set()
    for main_id, provider, attachments in ShareRevision.objects.values_list("main_sha_id", "provider", "attachments"):
        mains.add(main_id)
        atts.update((provider, sha) for sha in (attachments or {}).values() if isinstance(sha, str))
    return mains, atts


def gc(now=None):
    """Drop revisions older than the previous one, then blobs no kept revision
    uses that are older than an hour (younger ones may be a publish in progress).
    Returns ``(revisions, blobs)`` deleted."""
    now = now or timezone.now()
    revisions = 0
    keys = ShareRevision.objects.values_list("provider", "name").distinct()
    for provider, name in keys:
        keep = list(ShareRevision.objects.filter(provider=provider, name=name)
                    .order_by("-revision").values_list("revision", flat=True)[:KEEP])
        if keep:
            revisions += ShareRevision.objects.filter(provider=provider, name=name,
                                                      revision__lt=min(keep)).delete()[0]
    blobs = 0
    with transaction.atomic():
        candidates = list(ShareBlob.objects.select_for_update(skip_locked=True)
                          .filter(created__lt=now - PENDING_FOR).values_list("id", "owner", "sha256"))
        if candidates:
            # Read after locking, so a publish that committed meanwhile counts.
            mains, atts = _referenced()
            doomed = [pk for pk, owner, sha in candidates if pk not in mains and (owner, sha) not in atts]
            if doomed:
                blobs = ShareBlob.objects.filter(id__in=doomed).only("id").delete()[0]
    if revisions or blobs:
        log.info("share gc: %d revisions, %d blobs", revisions, blobs)
    return revisions, blobs


def forget(app_id):
    """``app_id`` was uninstalled: drop its shares (as provider) and grants."""
    with transaction.atomic():
        ShareRevision.objects.filter(provider=app_id).delete()
        ShareBlob.objects.filter(owner=app_id).only("id").delete()
        ShareGrant.objects.filter(provider=app_id).delete()
        ShareGrant.objects.filter(consumer=app_id).delete()


def installs_deleted(sender, instance, **kwargs):
    try:
        forget(instance.app_id)
    except Exception:
        log.exception("couldn't clean up the shares of %s", instance.app_id)
