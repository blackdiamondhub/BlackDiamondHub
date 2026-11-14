from django.contrib import admin

from .models import AppInstall, AuditEntry


@admin.register(AppInstall)
class AppInstallAdmin(admin.ModelAdmin):
    list_display = ("app_id", "version", "port", "upstream", "enabled", "manifest_fetched_at")
    list_filter = ("enabled",)
    readonly_fields = ("manifest", "manifest_fetched_at", "created", "updated")


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ("created", "app_id", "op", "allowed", "detail")
    list_filter = ("allowed", "app_id", "op")
    readonly_fields = ("created", "app_id", "op", "allowed", "detail")
