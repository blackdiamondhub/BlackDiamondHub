from django.apps import AppConfig


class DisplayConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "display"
    verbose_name = "Display"
    # When this process started serving; screens can't have checked in before it.
    ready_since = None

    def ready(self):
        from django.utils import timezone

        from hub import events

        DisplayConfig.ready_since = timezone.now()
        if events.should_start():
            from . import connect

            connect.start()
