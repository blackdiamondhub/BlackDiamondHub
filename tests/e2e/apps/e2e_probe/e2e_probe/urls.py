from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="e2e_probe_index"),
    path("bg.png", views.background, name="e2e_probe_bg"),
]
