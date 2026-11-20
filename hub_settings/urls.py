from django.urls import path

from . import apps_views, ha_views, views

urlpatterns = [
    path("", views.index, name="hub_settings"),
    path("location/", views.location_settings, name="hub_settings_location"),
    path("location/search/", views.location_search, name="hub_settings_location_search"),
    path("home/", views.home_layout, name="hub_settings_home"),
    path("quick/", views.quick_actions, name="hub_settings_quick"),
    path("home-assistant/", ha_views.ha_settings, name="hub_settings_ha"),
    path("home-assistant/test/", ha_views.ha_test, name="hub_settings_ha_test"),
    path("home-assistant/status/", ha_views.ha_status, name="hub_settings_ha_status"),
    path("apps/", apps_views.installed, name="hub_settings_apps"),
    path("apps/live/", apps_views.installed_live, name="hub_settings_apps_live"),
    path("apps/update-all/", apps_views.update_all, name="hub_settings_apps_update_all"),
    path("apps/store/", apps_views.store, name="hub_settings_store"),
    path("apps/store/<str:app_id>/", apps_views.store_app, name="hub_settings_store_app"),
    path("apps/<str:app_id>/manage/", apps_views.manage, name="hub_settings_app_manage"),
    path("apps/<str:app_id>/restart/", apps_views.manage_restart, name="hub_settings_app_restart"),
    path("apps/<str:app_id>/update/", apps_views.manage_update, name="hub_settings_app_update"),
    path("apps/<str:app_id>/auto-update/", apps_views.manage_auto_update, name="hub_settings_app_auto_update"),
    path("apps/<str:app_id>/permissions/", apps_views.manage_permissions, name="hub_settings_app_permissions"),
    path("apps/<str:app_id>/share-grant/", apps_views.manage_share_grant, name="hub_settings_app_share_grant"),
    path("apps/<str:app_id>/uninstall/", apps_views.manage_uninstall, name="hub_settings_app_uninstall"),
    path("app/<str:app_id>/", views.app_settings, name="hub_settings_app"),
    path("push/", views.push_stats, name="hub_settings_push"),
]
