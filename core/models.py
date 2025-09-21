from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid


class Tenant(models.Model):
    """
    Multi-tenant isolation model for Asterisk PBX instances
    """

    name = models.CharField(max_length=100, unique=True)
    domain = models.CharField(max_length=255, unique=True, null=True, blank=True)
    schema_name = models.CharField(max_length=63, unique=True)
    asterisk_host = models.CharField(max_length=255)
    asterisk_ami_port = models.IntegerField(default=5038)
    asterisk_ari_port = models.IntegerField(default=8088)
    asterisk_ami_username = models.CharField(max_length=50)
    asterisk_ami_secret = models.CharField(max_length=255)
    asterisk_ari_username = models.CharField(max_length=50)
    asterisk_ari_password = models.CharField(max_length=255)
    rtp_port_range_start = models.IntegerField(default=10000)
    rtp_port_range_end = models.IntegerField(default=20000)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tenants'
        ordering = ['name']

    def __str__(self):
        return self.name


class CallSession(models.Model):
    """
    Call session tracking with session-first approach
    """
    SESSION_STATUS_CHOICES = [
        ('detected', 'Call Detected'),
        ('answered', 'Call Answered'),
        ('bridged', 'Call Bridged'),
        ('recording', 'Recording Active'),
        ('ended', 'Call Ended'),
        ('failed', 'Call Failed'),
    ]

    CALL_DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='call_sessions')
    asterisk_channel_id = models.CharField(max_length=255, unique=True)
    asterisk_unique_id = models.CharField(max_length=255, unique=True)
    caller_id_name = models.CharField(max_length=100, null=True, blank=True)
    caller_id_number = models.CharField(max_length=50, null=True, blank=True)
    dialed_number = models.CharField(max_length=50, null=True, blank=True)
    direction = models.CharField(max_length=10, choices=CALL_DIRECTION_CHOICES)
    status = models.CharField(max_length=20, choices=SESSION_STATUS_CHOICES, default='detected')
    bridge_id = models.CharField(max_length=255, null=True, blank=True)
    external_media_channel_id = models.CharField(max_length=255, null=True, blank=True)
    rtp_endpoint_host = models.CharField(max_length=255, null=True, blank=True)
    rtp_endpoint_port = models.IntegerField(null=True, blank=True)
    session_metadata = models.JSONField(default=dict, blank=True)
    call_start_time = models.DateTimeField(default=timezone.now)
    call_answer_time = models.DateTimeField(null=True, blank=True)
    call_end_time = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'call_sessions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['asterisk_channel_id']),
            models.Index(fields=['asterisk_unique_id']),
        ]

    def __str__(self):
        return f"{self.caller_id_number} -> {self.dialed_number} ({self.status})"

    @property
    def duration(self):
        if self.call_end_time and self.call_answer_time:
            return (self.call_end_time - self.call_answer_time).total_seconds()
        return None


class AudioRecording(models.Model):
    """
    Audio recording metadata and storage tracking
    """
    RECORDING_STATUS_CHOICES = [
        ('starting', 'Recording Starting'),
        ('active', 'Recording Active'),
        ('stopped', 'Recording Stopped'),
        ('processed', 'Recording Processed'),
        ('failed', 'Recording Failed'),
    ]

    AUDIO_FORMAT_CHOICES = [
        ('wav', 'WAV'),
        ('mp3', 'MP3'),
        ('ulaw', 'μ-law'),
        ('alaw', 'A-law'),
    ]

    call_session = models.ForeignKey(CallSession, on_delete=models.CASCADE, related_name='audio_recordings')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='audio_recordings')
    file_path = models.CharField(max_length=500)
    file_size = models.BigIntegerField(null=True, blank=True)
    audio_format = models.CharField(max_length=10, choices=AUDIO_FORMAT_CHOICES, default='wav')
    sample_rate = models.IntegerField(default=8000)
    channels = models.IntegerField(default=1)
    duration_seconds = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=RECORDING_STATUS_CHOICES, default='starting')
    recording_metadata = models.JSONField(default=dict, blank=True)
    recording_start_time = models.DateTimeField(default=timezone.now)
    recording_end_time = models.DateTimeField(null=True, blank=True)
    transcription_text = models.TextField(null=True, blank=True)
    transcription_confidence = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'audio_recordings'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['call_session']),
        ]

    def __str__(self):
        return f"Recording for {self.call_session} ({self.status})"


class UserProfile(models.Model):
    """
    Extended user profile for multi-tenant permissions and roles
    """
    ROLE_CHOICES = [
        ('super_admin', 'Super Administrator'),
        ('tenant_admin', 'Tenant Administrator'),
        ('operator', 'Call Operator'),
        ('viewer', 'Call Viewer'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='user_profiles', null=True, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')
    phone_extension = models.CharField(max_length=20, null=True, blank=True)
    department = models.CharField(max_length=100, null=True, blank=True)
    can_monitor_calls = models.BooleanField(default=False)
    can_record_calls = models.BooleanField(default=False)
    can_access_recordings = models.BooleanField(default=False)
    can_manage_users = models.BooleanField(default=False)
    can_view_analytics = models.BooleanField(default=False)
    permissions_metadata = models.JSONField(default=dict, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_profiles'
        ordering = ['user__username']
        indexes = [
            models.Index(fields=['tenant', 'role']),
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def is_admin(self):
        return self.role in ['super_admin', 'tenant_admin']


class SystemConfiguration(models.Model):
    """
    System-wide and tenant-specific configuration settings
    """
    CONFIG_SCOPE_CHOICES = [
        ('global', 'Global'),
        ('tenant', 'Tenant-Specific'),
    ]

    CONFIG_TYPE_CHOICES = [
        ('string', 'String'),
        ('integer', 'Integer'),
        ('float', 'Float'),
        ('boolean', 'Boolean'),
        ('json', 'JSON'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='configurations', null=True, blank=True)
    scope = models.CharField(max_length=10, choices=CONFIG_SCOPE_CHOICES, default='global')
    key = models.CharField(max_length=100)
    value = models.TextField()
    value_type = models.CharField(max_length=10, choices=CONFIG_TYPE_CHOICES, default='string')
    description = models.TextField(null=True, blank=True)
    is_encrypted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'system_configurations'
        ordering = ['scope', 'key']
        unique_together = [
            ['tenant', 'key'],  # Each tenant + key must be unique
            # REMOVE ['scope', 'key']
        ]
        indexes = [
            models.Index(fields=['tenant', 'scope']),
        ]

    def __str__(self):
        scope_display = f"{self.tenant.name}" if self.tenant else "Global"
        return f"{scope_display}: {self.key}"

    def get_typed_value(self):
        """Convert string value to appropriate Python type"""
        if self.value_type == 'boolean':
            return self.value.lower() in ['true', '1', 'yes', 'on']
        elif self.value_type == 'integer':
            return int(self.value)
        elif self.value_type == 'float':
            return float(self.value)
        elif self.value_type == 'json':
            import json
            return json.loads(self.value)
        else:
            return self.value
