"""
Tenant ViewSet for aiMediaGateway

This module contains ViewSets for the Tenant model with proper filtering,
permissions, and multi-tenant access control.
"""

from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Count, Q
from django.utils import timezone
from datetime import timedelta

from core.models import Tenant, CallSession, UserProfile
from core.serializers import TenantSerializer, TenantStatsSerializer
from core.permissions import TenantAdminPermission, TenantResourcePermission
from core.filters import TenantFilterSet


class TenantViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tenants with proper filtering and permissions.
    
    Provides CRUD operations for tenant management with role-based access control
    and comprehensive filtering capabilities.
    """
    
    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated, TenantAdminPermission]
    
    # Advanced filtering and search configuration
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_class = TenantFilterSet
    search_fields = ['name', 'slug', 'subdomain', 'domain']
    ordering_fields = ['name', 'created_at', 'updated_at', 'user_count', 'session_count']
    ordering = ['name']
    
    def get_queryset(self):
        """
        Filter queryset based on user permissions and tenant context.
        
        Returns:
            QuerySet: Filtered tenant queryset
        """
        queryset = super().get_queryset()
        
        # Add related counts for efficiency
        queryset = queryset.annotate(
            user_count=Count('userprofile', distinct=True),
            session_count=Count('callsession', distinct=True),
            active_session_count=Count(
                'callsession',
                filter=Q(callsession__status__in=['active', 'ringing', 'connecting']),
                distinct=True
            )
        )
        
        # Filter based on user role
        if hasattr(self.request, 'user') and self.request.user.is_authenticated:
            try:
                user_profile = UserProfile.objects.get(user=self.request.user)
                
                # System admins can see all tenants
                if user_profile.role == 'admin':
                    return queryset
                
                # Tenant admins can only see their own tenant
                elif user_profile.role == 'tenant_admin':
                    return queryset.filter(id=user_profile.tenant.id)
                
                # Other roles cannot manage tenants
                else:
                    return queryset.none()
                    
            except UserProfile.DoesNotExist:
                return queryset.none()
        
        return queryset.none()
    
    def perform_create(self, serializer):
        """
        Create a new tenant with proper defaults.
        
        Args:
            serializer: TenantSerializer instance
        """
        # Set default configuration if not provided
        if not serializer.validated_data.get('configuration'):
            serializer.validated_data['configuration'] = {
                'recording_enabled': True,
                'transcription_enabled': False,
                'max_concurrent_calls': 100,
                'storage_limit_mb': 10240,  # 10GB default
                'retention_days': 90,
            }
        
        serializer.save()
    
    def perform_update(self, serializer):
        """
        Update tenant with audit logging.
        
        Args:
            serializer: TenantSerializer instance
        """
        old_instance = self.get_object()
        new_instance = serializer.save()
        
        # Log significant changes
        if old_instance.is_active != new_instance.is_active:
            action_type = 'activated' if new_instance.is_active else 'deactivated'
            # TODO: Add audit logging
            print(f"Tenant {new_instance.name} {action_type} by {self.request.user.username}")
    
    def perform_destroy(self, instance):
        """
        Soft delete tenant instead of hard delete.
        
        Args:
            instance: Tenant instance to delete
        """
        # Instead of deleting, deactivate the tenant
        instance.is_active = False
        instance.save()
        
        # TODO: Add audit logging
        print(f"Tenant {instance.name} deactivated by {self.request.user.username}")
    
    @action(detail=True, methods=['get'])
    def statistics(self, request, pk=None):
        """
        Get detailed statistics for a specific tenant.
        
        Args:
            request: HTTP request
            pk: Tenant primary key
            
        Returns:
            Response: Tenant statistics data
        """
        tenant = self.get_object()
        
        # Calculate date ranges
        now = timezone.now()
        today = now.date()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        
        # User statistics
        users = UserProfile.objects.filter(tenant=tenant)
        total_users = users.count()
        active_users = users.filter(user__is_active=True).count()
        admin_users = users.filter(role__in=['admin', 'tenant_admin']).count()
        
        # Call session statistics
        sessions = CallSession.objects.filter(tenant=tenant)
        total_sessions = sessions.count()
        active_sessions = sessions.filter(
            status__in=['active', 'ringing', 'connecting']
        ).count()
        completed_sessions = sessions.filter(status='completed').count()
        failed_sessions = sessions.filter(status='failed').count()
        
        # Time-based session statistics
        sessions_today = sessions.filter(start_time__date=today).count()
        sessions_this_week = sessions.filter(start_time__gte=week_ago).count()
        sessions_this_month = sessions.filter(start_time__gte=month_ago).count()
        
        # Audio recording statistics
        recordings = tenant.callsession_set.values_list('audiorecording', flat=True)
        total_recordings = len([r for r in recordings if r])
        
        # Calculate average metrics
        completed_session_durations = sessions.filter(
            status='completed',
            end_time__isnull=False
        ).values_list('start_time', 'end_time')
        
        if completed_session_durations:
            durations = [
                (end - start).total_seconds()
                for start, end in completed_session_durations
            ]
            average_call_duration = sum(durations) / len(durations)
        else:
            average_call_duration = None
        
        # Last activity
        last_session = sessions.order_by('-created_at').first()
        last_activity = last_session.created_at if last_session else None
        
        stats_data = {
            'tenant_id': tenant.id,
            'tenant_name': tenant.name,
            'tenant_slug': tenant.slug,
            'total_users': total_users,
            'active_users': active_users,
            'admin_users': admin_users,
            'total_sessions': total_sessions,
            'active_sessions': active_sessions,
            'completed_sessions': completed_sessions,
            'failed_sessions': failed_sessions,
            'sessions_today': sessions_today,
            'sessions_this_week': sessions_this_week,
            'sessions_this_month': sessions_this_month,
            'total_recordings': total_recordings,
            'total_recording_size_mb': 0.0,  # TODO: Calculate from actual files
            'transcribed_recordings': 0,  # TODO: Calculate from transcription data
            'average_call_duration': average_call_duration,
            'average_recording_size_mb': None,
            'transcription_success_rate': None,
            'storage_used_mb': 0.0,  # TODO: Calculate from actual storage
            'bandwidth_used_mb': None,
            'last_activity': last_activity,
            'stats_generated_at': now,
        }
        
        serializer = TenantStatsSerializer(data=stats_data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """
        Activate a tenant.
        
        Args:
            request: HTTP request
            pk: Tenant primary key
            
        Returns:
            Response: Success or error message
        """
        tenant = self.get_object()
        
        if tenant.is_active:
            return Response(
                {'message': 'Tenant is already active'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        tenant.is_active = True
        tenant.save()
        
        # TODO: Add audit logging
        print(f"Tenant {tenant.name} activated by {request.user.username}")
        
        return Response({'message': 'Tenant activated successfully'})
    
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """
        Deactivate a tenant.
        
        Args:
            request: HTTP request
            pk: Tenant primary key
            
        Returns:
            Response: Success or error message
        """
        tenant = self.get_object()
        
        if not tenant.is_active:
            return Response(
                {'message': 'Tenant is already inactive'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if tenant has active sessions
        active_sessions = CallSession.objects.filter(
            tenant=tenant,
            status__in=['active', 'ringing', 'connecting']
        ).count()
        
        if active_sessions > 0:
            return Response(
                {
                    'message': f'Cannot deactivate tenant with {active_sessions} active sessions',
                    'active_sessions': active_sessions
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        tenant.is_active = False
        tenant.save()
        
        # TODO: Add audit logging
        print(f"Tenant {tenant.name} deactivated by {request.user.username}")
        
        return Response({'message': 'Tenant deactivated successfully'})
    
    @action(detail=False, methods=['get'])
    def my_tenant(self, request):
        """
        Get the current user's tenant information.
        
        Args:
            request: HTTP request
            
        Returns:
            Response: Current tenant data
        """
        if hasattr(request, 'tenant') and request.tenant:
            serializer = self.get_serializer(request.tenant)
            return Response(serializer.data)
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            serializer = self.get_serializer(user_profile.tenant)
            return Response(serializer.data)
        except UserProfile.DoesNotExist:
            return Response(
                {'message': 'User profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )
