"""
Serializers package for aiMediaGateway core models

This package contains Django REST Framework serializers for the core models
in the multi-tenant Asterisk PBX management system.
"""

# Import all serializers to make them available from the package
from .tenant import TenantSerializer
from .user_profile import UserProfileSerializer, UserPermissionSerializer
from .call_session import (
    CallSessionSerializer, 
    CallSessionDetailSerializer, 
    CallSessionCreateSerializer, 
    LiveCallStatusSerializer
)
from .audio_recording import AudioRecordingSerializer, AudioTranscriptionSerializer
from .system_configuration import SystemConfigurationSerializer, TenantStatsSerializer

__all__ = [
    'TenantSerializer',
    'UserProfileSerializer',
    'UserPermissionSerializer',
    'CallSessionSerializer',
    'CallSessionDetailSerializer', 
    'CallSessionCreateSerializer',
    'LiveCallStatusSerializer',
    'AudioRecordingSerializer',
    'AudioTranscriptionSerializer',
    'SystemConfigurationSerializer',
    'TenantStatsSerializer',
]
