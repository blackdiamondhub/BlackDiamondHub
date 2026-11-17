from django.apps import AppConfig


class E2eProbeConfig(AppConfig):
    name = "e2e_probe"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .plugin import VARIANT

        if VARIANT and VARIANT.get("subscribe"):
            from bdh_sdk import events

            from .views import record_event

            for pattern in VARIANT["subscribe"]:
                events.subscribe(pattern, record_event)