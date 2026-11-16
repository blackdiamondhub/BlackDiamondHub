from django.apps import AppConfig


class HubSettingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hub_settings"
    verbose_name = "Hub settings"

    def ready(self):
        from bdh_sdk.secrets import set_backend as set_secret_backend
        from bdh_sdk.settings import set_store
        from bdh_sdk.storage import set_state_backend
        from django.db.models.signals import post_delete, post_save

        from . import store
        from .models import AppSecret, AppSetting
        from .state import DatabaseStateBackend
        from .vault import DatabaseSecretBackend

        set_store(store.lookup)
        set_state_backend(DatabaseStateBackend())
        set_secret_backend(DatabaseSecretBackend())
        post_save.connect(store.clear, sender=AppSetting, dispatch_uid="hub_settings_clear_save")
        post_delete.connect(store.clear, sender=AppSetting, dispatch_uid="hub_settings_clear_delete")
        post_save.connect(store.clear, sender=AppSecret, dispatch_uid="hub_settings_clear_secret_save")
        post_delete.connect(store.clear, sender=AppSecret, dispatch_uid="hub_settings_clear_secret_delete")

        from bdh_sdk import ha as sdk_ha

        from . import ha
        from .models import HAInstance

        sdk_ha.set_provider(ha.HubProvider())
        post_save.connect(ha.clear, sender=HAInstance, dispatch_uid="hub_settings_ha_save")
        post_delete.connect(ha.clear, sender=HAInstance, dispatch_uid="hub_settings_ha_delete")
        post_save.connect(ha.clear, sender=AppSecret, dispatch_uid="hub_settings_ha_secret_save")
        post_delete.connect(ha.clear, sender=AppSecret, dispatch_uid="hub_settings_ha_secret_delete")
