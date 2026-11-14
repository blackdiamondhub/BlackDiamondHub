"""App tokens: derived from SECRET_KEY, so the hub stores only a generation."""
import hmac

from bdh_sdk.identity import app_token
from django.conf import settings

from .models import AppInstall


def token_for(install):
    return app_token(settings.SECRET_KEY, install.app_id, install.generation)


def bearer(header):
    scheme, _, token = (header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def authenticate(token):
    """The enabled install ``token`` belongs to, or None.

    The SDK's client sends only the token, so every enabled app is checked
    (there are a handful), each with a constant-time compare.
    """
    if not token:
        return None
    found = None
    for install in AppInstall.objects.filter(enabled=True):
        if hmac.compare_digest(token_for(install), token):
            found = install
    return found
