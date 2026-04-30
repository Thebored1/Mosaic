from django.contrib import admin

from .models import AuditEvent, AuditEventLink


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'event_type', 'action', 'organization', 'actor_user', 'outcome', 'status_code']
    list_filter = ['event_type', 'source_app', 'outcome', 'organization']
    search_fields = ['trace_id', 'correlation_id', 'object_repr', 'request_path', 'error_message']
    readonly_fields = [field.name for field in AuditEvent._meta.fields]


@admin.register(AuditEventLink)
class AuditEventLinkAdmin(admin.ModelAdmin):
    list_display = ['id', 'event', 'relation_type', 'object_repr']
    list_filter = ['relation_type', 'content_type']
    search_fields = ['object_repr', 'object_id']
    readonly_fields = [field.name for field in AuditEventLink._meta.fields]
