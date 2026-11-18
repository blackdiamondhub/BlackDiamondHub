from bdh_sdk.provides import Background
from django.urls import reverse


def backgrounds():
    return [Background("probe", "Probe Cam", reverse("e2e_probe_bg"), refresh_seconds=60)]
