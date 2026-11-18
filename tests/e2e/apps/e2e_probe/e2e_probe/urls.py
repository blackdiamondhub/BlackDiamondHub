from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="e2e_probe_index"),
    path("bg.png", views.background, name="e2e_probe_bg"),
    path("files/<slug:name>", views.files, name="e2e_probe_files"),
    path("events", views.events_log, name="e2e_probe_events"),
    path("status-log", views.status_log, name="e2e_probe_status_log"),
    path("companion", views.companion, name="e2e_probe_companion"),
    path("rpc/<str:op>", views.rpc, name="e2e_probe_rpc"),
]