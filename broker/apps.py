from django.apps import AppConfig


class BrokerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "broker"
    verbose_name = "App broker"

    def ready(self):
        from django.db.models.signals import post_delete, post_save

        from . import remote
        from .models import AppInstall

        post_save.connect(remote.installs_changed, sender=AppInstall, dispatch_uid="broker_install_saved")
        post_delete.connect(remote.installs_changed, sender=AppInstall, dispatch_uid="broker_install_deleted")

        from . import shares

        post_delete.connect(shares.installs_deleted, sender=AppInstall, dispatch_uid="broker_install_shares")

        from hub import events

        if events.is_server():
            from . import events_bridge

            events_bridge.start()
