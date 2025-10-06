"""
ViewSets package for aiMediaGateway core API

This package contains Django REST Framework ViewSets for the core models
in the multi-tenant Asterisk PBX management system.
"""

# Import all implemented ViewSets
from .tenant import TenantViewSet
from .user_profile import UserProfileViewSet
from .call_session import CallSessionViewSet
from .audio_recording import AudioRecordingViewSet
from .system_configuration import SystemConfigurationViewSet

__all__ = [
    'TenantViewSet',
    'UserProfileViewSet',
    'CallSessionViewSet',
    'AudioRecordingViewSet',
    'SystemConfigurationViewSet',
]
