from bdh_sdk.provides import SaverImage
from django.urls import reverse


def saver():
    return [SaverImage("cam", "probe", reverse("e2e_probe_bg"), label="Probe Cam", refresh_seconds=60)]
