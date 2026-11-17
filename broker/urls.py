from django.urls import path

from . import views

app_name = "broker"

urlpatterns = [
    path("_nav/", views.navigate, name="navigate"),
    path("_share/<str:sha>", views.share_blob, name="share_blob"),
    path("<slug:app_id>/_bare/", views.bare, name="bare_root"),
    path("<slug:app_id>/_bare/<path:path>", views.bare, name="bare"),
    path("<slug:app_id>/_raw/<path:path>", views.raw, name="raw"),
    path("<slug:app_id>/", views.shell, name="shell"),
    path("<slug:app_id>/<path:path>", views.shell, name="shell_path"),
]
