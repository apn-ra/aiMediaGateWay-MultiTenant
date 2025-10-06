"""
URL configuration for API v1

This module defines the URL patterns for version 1 of the aiMediaGateway API.
It uses Django REST Framework's DefaultRouter for automatic ViewSet routing.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenBlacklistView,
)

# Import all ViewSets
from core.viewsets import (
    TenantViewSet,
    UserProfileViewSet,
    CallSessionViewSet,
    AudioRecordingViewSet,
    SystemConfigurationViewSet,
)

# Create the main API router
router = DefaultRouter()

# Register all ViewSets with appropriate URL patterns
router.register(r'tenants', TenantViewSet)
router.register(r'users', UserProfileViewSet)
router.register(r'call-sessions', CallSessionViewSet)
router.register(r'audio-recordings', AudioRecordingViewSet)
router.register(r'configurations', SystemConfigurationViewSet)

app_name = 'api_v1'

urlpatterns = [
    # Include the router URLs
    path('', include(router.urls)),
    
    # JWT Authentication endpoints
    path('auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/token/blacklist/', TokenBlacklistView.as_view(), name='token_blacklist'),
    
    # Additional custom endpoints can be added here
    # path('auth/', include('rest_framework.urls')),
]
