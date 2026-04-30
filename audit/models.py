from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class AuditEvent(models.Model):
    OUTCOME_CHOICES = [
        ('success', 'Success'),
        ('failure', 'Failure'),
    ]

    trace_id = models.CharField(max_length=64, db_index=True, blank=True, default='')
    correlation_id = models.CharField(max_length=64, db_index=True, blank=True, default='')
    event_type = models.CharField(max_length=120, db_index=True)
    action = models.CharField(max_length=120, blank=True, default='')
    source_app = models.CharField(max_length=50, blank=True, default='')
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_events',
    )
    actor_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_events',
    )
    actor_account = models.ForeignKey(
        'account.UserAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_events',
    )
    request_method = models.CharField(max_length=10, blank=True, default='')
    request_path = models.CharField(max_length=255, blank=True, default='')
    request_query = models.TextField(blank=True, default='')
    status_code = models.PositiveIntegerField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default='')
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    object_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    object_repr = models.CharField(max_length=255, blank=True, default='')
    before_state = models.JSONField(default=dict, blank=True)
    after_state = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, default='success')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['trace_id', 'created_at']),
            models.Index(fields=['correlation_id', 'created_at']),
            models.Index(fields=['organization', 'created_at']),
            models.Index(fields=['actor_user', 'created_at']),
            models.Index(fields=['event_type', 'created_at']),
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f'{self.event_type} - {self.object_repr or self.object_id}'


class AuditEventLink(models.Model):
    RELATION_CHOICES = [
        ('primary', 'Primary'),
        ('related', 'Related'),
        ('source', 'Source'),
        ('request', 'Request'),
    ]

    event = models.ForeignKey(AuditEvent, on_delete=models.CASCADE, related_name='links')
    relation_type = models.CharField(max_length=20, choices=RELATION_CHOICES, default='related')
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=64, db_index=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    object_repr = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
            models.Index(fields=['relation_type']),
        ]

    def __str__(self):
        return f'{self.event_id} -> {self.object_repr or self.object_id}'
