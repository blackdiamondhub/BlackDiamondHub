from django.apps import AppConfig


class DisplayConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "display"
    verbose_name = "Display"

    def ready(self):
        from hub import events

        if events.should_start():
            from . import connect

            connect.start()
