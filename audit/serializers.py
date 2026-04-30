from rest_framework import serializers

from .models import AuditEvent, AuditEventLink


class AuditEventLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEventLink
        fields = [
            'id',
            'relation_type',
            'content_type',
            'object_id',
            'object_repr',
            'metadata',
        ]


class AuditEventSerializer(serializers.ModelSerializer):
    links = AuditEventLinkSerializer(many=True, read_only=True)
    actor_username = serializers.CharField(source='actor_user.username', read_only=True)
    actor_account_role = serializers.CharField(source='actor_account.role', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    model_label = serializers.SerializerMethodField()

    class Meta:
        model = AuditEvent
        fields = [
            'id',
            'trace_id',
            'correlation_id',
            'event_type',
            'action',
            'source_app',
            'organization',
            'organization_name',
            'actor_user',
            'actor_username',
            'actor_account',
            'actor_account_role',
            'request_method',
            'request_path',
            'request_query',
            'status_code',
            'ip_address',
            'user_agent',
            'duration_ms',
            'content_type',
            'object_id',
            'object_repr',
            'before_state',
            'after_state',
            'metadata',
            'outcome',
            'error_message',
            'created_at',
            'model_label',
            'links',
        ]
        read_only_fields = fields

    def get_model_label(self, obj):
        if obj.content_type_id:
            return f'{obj.content_type.app_label}.{obj.content_type.model}'
        return ''

