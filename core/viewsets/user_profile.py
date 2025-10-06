"""
UserProfile ViewSet for aiMediaGateway

Provides CRUD operations for UserProfile management with tenant isolation.
Includes role-based permissions and user management functionality.
"""

from rest_framework import viewsets, filters, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q, Count
from django.contrib.auth.models import User
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiExample, OpenApiResponse
from drf_spectacular.types import OpenApiTypes

from core.models import UserProfile, Tenant, CallSession
from core.serializers import UserProfileSerializer, UserPermissionSerializer
from core.permissions import (
    TenantAdminPermission, 
    TenantResourcePermission, 
    MatrixBasedPermission,
    get_user_effective_permissions,
    check_permission,
    get_allowed_fields,
    PERMISSION_MATRIX,
    ROLE_HIERARCHY
)


@extend_schema_view(
    list=extend_schema(
        summary="List user profiles",
        description="Retrieve a list of user profiles within the current tenant context with filtering and pagination support.",
        parameters=[
            OpenApiParameter(
                name='role',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description='Filter by user role',
                enum=['super_admin', 'tenant_admin', 'operator', 'viewer']
            ),
            OpenApiParameter(
                name='is_active',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description='Filter by active status'
            ),
            OpenApiParameter(
                name='search',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description='Search by username or email'
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=UserProfileSerializer(many=True),
                description="List of user profiles with pagination"
            )
        },
        tags=['Users']
    ),
    create=extend_schema(
        summary="Create user profile",
        description="Create a new user profile within the current tenant context.",
        request=UserProfileSerializer,
        responses={
            201: UserProfileSerializer,
            400: OpenApiResponse(description="Validation errors"),
            403: OpenApiResponse(description="Permission denied")
        },
        tags=['Users']
    ),
    retrieve=extend_schema(
        summary="Get user profile",
        description="Retrieve a specific user profile by ID.",
        responses={
            200: UserProfileSerializer,
            404: OpenApiResponse(description="User profile not found")
        },
        tags=['Users']
    ),
    update=extend_schema(
        summary="Update user profile", 
        description="Update a user profile with tenant admin permissions.",
        request=UserProfileSerializer,
        responses={
            200: UserProfileSerializer,
            400: OpenApiResponse(description="Validation errors"),
            403: OpenApiResponse(description="Permission denied"),
            404: OpenApiResponse(description="User profile not found")
        },
        tags=['Users']
    ),
    destroy=extend_schema(
        summary="Delete user profile",
        description="Delete a user profile (admin only).",
        responses={
            204: OpenApiResponse(description="User profile deleted successfully"),
            403: OpenApiResponse(description="Permission denied"),
            404: OpenApiResponse(description="User profile not found")
        },
        tags=['Users']
    )
)
class UserProfileViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing UserProfiles with tenant isolation.
    
    Provides:
    - CRUD operations for user profiles
    - Tenant-scoped user management
    - Role-based access control
    - User statistics and activity tracking
    """
    
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated, TenantResourcePermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['role', 'is_active', 'created_at']
    search_fields = ['user__username', 'user__first_name', 'user__last_name', 'user__email', 'phone_number']
    ordering_fields = ['created_at', 'updated_at', 'user__username', 'user__last_login']
    ordering = ['-created_at']

    def get_queryset(self):
        """
        Filter users by current user's tenant context.
        Admins see all users in tenant, others see limited view.
        """
        if not hasattr(self.request, 'tenant') or not self.request.tenant:
            return UserProfile.objects.none()

        queryset = UserProfile.objects.filter(tenant=self.request.tenant).select_related('user', 'tenant')
        
        # Non-admin users can only see active users and themselves
        user_profile = getattr(self.request.user, 'userprofile', None)
        if user_profile and user_profile.role not in ['tenant_admin', 'super_admin']:
            queryset = queryset.filter(
                Q(is_active=True) | Q(user=self.request.user)
            )
        
        return queryset

    def get_permissions(self):
        """
        Instantiate and return the list of permissions that this view requires.
        Admin actions require TenantAdminPermission.
        """
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'change_role', 'deactivate', 'activate']:
            permission_classes = [IsAuthenticated, TenantAdminPermission]
        else:
            permission_classes = [IsAuthenticated, TenantResourcePermission]
        
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        """
        Create a new user profile within the current tenant.
        """
        if not hasattr(self.request, 'tenant') or not self.request.tenant:
            raise serializers.ValidationError("Tenant context is required")
        
        # Auto-assign tenant context
        serializer.save(
            tenant=self.request.tenant,
            created_by=self.request.user
        )

    def perform_update(self, serializer):
        """
        Update user profile with audit trail.
        """
        serializer.save(
            updated_by=self.request.user,
            updated_at=timezone.now()
        )

    def perform_destroy(self, instance):
        """
        Soft delete user profile instead of hard delete.
        """
        instance.is_active = False
        instance.deleted_at = timezone.now()
        instance.deleted_by = self.request.user
        instance.save()

    @action(detail=False, methods=['get'])
    def active(self, request):
        """
        Get all active user profiles in the tenant.
        """
        queryset = self.get_queryset().filter(is_active=True)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_role(self, request):
        """
        Get users grouped by role.
        """
        role_filter = request.query_params.get('role', None)
        queryset = self.get_queryset()
        
        if role_filter:
            queryset = queryset.filter(role=role_filter)
        
        # Group users by role
        roles_data = {}
        for user_profile in queryset:
            role = user_profile.role
            if role not in roles_data:
                roles_data[role] = []
            
            serializer = self.get_serializer(user_profile)
            roles_data[role].append(serializer.data)
        
        return Response(roles_data)

    @action(detail=True, methods=['post'])
    def change_role(self, request, pk=None):
        """
        Change user role with validation.
        """
        user_profile = self.get_object()
        serializer = UserPermissionSerializer(data=request.data)
        
        if serializer.is_valid():
            new_role = serializer.validated_data['new_role']
            old_role = user_profile.role
            
            # Update role
            user_profile.role = new_role
            user_profile.updated_by = request.user
            user_profile.updated_at = timezone.now()
            user_profile.save()
            
            return Response({
                'message': f'User role changed from {old_role} to {new_role}',
                'user_id': user_profile.user.id,
                'old_role': old_role,
                'new_role': new_role
            })
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """
        Activate a user profile.
        """
        user_profile = self.get_object()
        
        if user_profile.is_active:
            return Response({
                'message': 'User is already active'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        user_profile.is_active = True
        user_profile.updated_by = request.user
        user_profile.updated_at = timezone.now()
        user_profile.save()
        
        return Response({
            'message': 'User profile activated successfully',
            'user_id': user_profile.user.id,
            'username': user_profile.user.username
        })

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """
        Deactivate a user profile.
        """
        user_profile = self.get_object()
        
        if not user_profile.is_active:
            return Response({
                'message': 'User is already inactive'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Prevent self-deactivation
        if user_profile.user == request.user:
            return Response({
                'error': 'Cannot deactivate your own account'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        user_profile.is_active = False
        user_profile.updated_by = request.user
        user_profile.updated_at = timezone.now()
        user_profile.save()
        
        return Response({
            'message': 'User profile deactivated successfully',
            'user_id': user_profile.user.id,
            'username': user_profile.user.username
        })

    @action(detail=True, methods=['get'])
    def activity(self, request, pk=None):
        """
        Get user activity statistics including call sessions.
        """
        user_profile = self.get_object()
        
        # Get call session statistics
        call_sessions = CallSession.objects.filter(
            caller_user=user_profile.user,
            tenant=self.request.tenant
        )
        
        total_calls = call_sessions.count()
        active_calls = call_sessions.filter(status='active').count()
        completed_calls = call_sessions.filter(status='completed').count()
        
        # Calculate total call duration (in minutes)
        total_duration = 0
        for session in call_sessions.filter(status='completed'):
            if session.start_time and session.end_time:
                duration = (session.end_time - session.start_time).total_seconds() / 60
                total_duration += duration
        
        activity_data = {
            'user_id': user_profile.user.id,
            'username': user_profile.user.username,
            'role': user_profile.role,
            'last_login': user_profile.user.last_login,
            'is_active': user_profile.is_active,
            'call_statistics': {
                'total_calls': total_calls,
                'active_calls': active_calls,
                'completed_calls': completed_calls,
                'total_duration_minutes': round(total_duration, 2)
            },
            'profile_created': user_profile.created_at,
            'last_updated': user_profile.updated_at
        }
        
        return Response(activity_data)

    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """
        Get tenant-wide user statistics.
        """
        queryset = self.get_queryset()
        
        total_users = queryset.count()
        active_users = queryset.filter(is_active=True).count()
        inactive_users = total_users - active_users
        
        # Role distribution
        role_counts = {}
        role_choices = ['tenant_admin', 'call_operator', 'view_only', 'super_admin']
        for role in role_choices:
            role_counts[role] = queryset.filter(role=role).count()
        
        # Recent activity (users created in last 30 days)
        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        recent_users = queryset.filter(created_at__gte=thirty_days_ago).count()
        
        statistics_data = {
            'total_users': total_users,
            'active_users': active_users,
            'inactive_users': inactive_users,
            'role_distribution': role_counts,
            'recent_users_30_days': recent_users,
            'tenant_name': self.request.tenant.name if self.request.tenant else 'Unknown',
            'generated_at': timezone.now()
        }
        
        return Response(statistics_data)

    @action(detail=False, methods=['get'])
    def my_profile(self, request):
        """
        Get current user's profile information.
        """
        try:
            user_profile = UserProfile.objects.get(
                user=request.user,
                tenant=self.request.tenant
            )
            serializer = self.get_serializer(user_profile)
            return Response(serializer.data)
        except UserProfile.DoesNotExist:
            return Response({
                'error': 'User profile not found'
            }, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['get'])
    def permissions_matrix(self, request):
        """
        Get the complete permission matrix for all roles.
        """
        # Only allow tenant admins and super admins to view permission matrix
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            if user_profile.role not in ['super_admin', 'tenant_admin']:
                return Response({
                    'error': 'Insufficient permissions to view permission matrix'
                }, status=status.HTTP_403_FORBIDDEN)
        except UserProfile.DoesNotExist:
            return Response({
                'error': 'User profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        return Response({
            'permission_matrix': PERMISSION_MATRIX,
            'role_hierarchy': ROLE_HIERARCHY,
            'generated_at': timezone.now()
        })
    
    @action(detail=True, methods=['get'])
    def user_permissions(self, request, pk=None):
        """
        Get effective permissions for a specific user.
        """
        user_profile = self.get_object()
        
        # Build permissions for all models
        user_permissions = {}
        for model_name in ['Tenant', 'UserProfile', 'CallSession', 'AudioRecording', 'SystemConfiguration', 'AudioTranscription']:
            effective_permissions = get_user_effective_permissions(user_profile.role, model_name)
            user_permissions[model_name] = list(effective_permissions)
        
        # Get allowed fields for each model
        allowed_fields = {}
        for model_name in user_permissions.keys():
            allowed_fields[model_name] = list(get_allowed_fields(user_profile.role, model_name))
        
        return Response({
            'user_id': user_profile.user.id,
            'username': user_profile.user.username,
            'role': user_profile.role,
            'effective_permissions': user_permissions,
            'allowed_fields': allowed_fields,
            'is_active': user_profile.is_active,
            'generated_at': timezone.now()
        })
    
    @action(detail=True, methods=['post'])
    def validate_role_change(self, request, pk=None):
        """
        Validate if a role change is allowed and show impact.
        """
        user_profile = self.get_object()
        new_role = request.data.get('new_role')
        
        if not new_role:
            return Response({
                'error': 'new_role is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate new role exists in permission matrix
        if new_role not in PERMISSION_MATRIX:
            return Response({
                'error': f'Invalid role: {new_role}',
                'valid_roles': list(PERMISSION_MATRIX.keys())
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if current user has permission to change roles
        try:
            current_user_profile = UserProfile.objects.get(user=request.user)
            if not check_permission(current_user_profile.role, 'UserProfile', 'update'):
                return Response({
                    'error': 'Insufficient permissions to change user roles'
                }, status=status.HTTP_403_FORBIDDEN)
        except UserProfile.DoesNotExist:
            return Response({
                'error': 'Current user profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Show permission changes
        old_permissions = {}
        new_permissions = {}
        
        for model_name in ['Tenant', 'UserProfile', 'CallSession', 'AudioRecording', 'SystemConfiguration']:
            old_permissions[model_name] = list(get_user_effective_permissions(user_profile.role, model_name))
            new_permissions[model_name] = list(get_user_effective_permissions(new_role, model_name))
        
        return Response({
            'user_id': user_profile.user.id,
            'username': user_profile.user.username,
            'current_role': user_profile.role,
            'new_role': new_role,
            'validation_result': 'valid',
            'permission_changes': {
                'current_permissions': old_permissions,
                'new_permissions': new_permissions
            },
            'generated_at': timezone.now()
        })
    
    @action(detail=False, methods=['post'])
    def bulk_role_change(self, request):
        """
        Change roles for multiple users at once.
        """
        user_ids = request.data.get('user_ids', [])
        new_role = request.data.get('new_role')
        
        if not user_ids or not new_role:
            return Response({
                'error': 'user_ids and new_role are required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate new role
        if new_role not in PERMISSION_MATRIX:
            return Response({
                'error': f'Invalid role: {new_role}',
                'valid_roles': list(PERMISSION_MATRIX.keys())
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check permissions
        try:
            current_user_profile = UserProfile.objects.get(user=request.user)
            if current_user_profile.role not in ['super_admin', 'tenant_admin']:
                return Response({
                    'error': 'Insufficient permissions for bulk role changes'
                }, status=status.HTTP_403_FORBIDDEN)
        except UserProfile.DoesNotExist:
            return Response({
                'error': 'Current user profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Perform bulk update
        queryset = self.get_queryset().filter(user__id__in=user_ids)
        updated_count = 0
        results = []
        
        for user_profile in queryset:
            old_role = user_profile.role
            user_profile.role = new_role
            user_profile.updated_by = request.user
            user_profile.updated_at = timezone.now()
            user_profile.save()
            
            updated_count += 1
            results.append({
                'user_id': user_profile.user.id,
                'username': user_profile.user.username,
                'old_role': old_role,
                'new_role': new_role
            })
        
        return Response({
            'message': f'Successfully updated {updated_count} user roles',
            'updated_users': results,
            'total_requested': len(user_ids),
            'total_updated': updated_count
        })
    
    @action(detail=False, methods=['get'])
    def tenant_admins(self, request):
        """
        Get all tenant administrators for current tenant.
        """
        try:
            current_user_profile = UserProfile.objects.get(user=request.user)
            if current_user_profile.role not in ['super_admin', 'tenant_admin']:
                return Response({
                    'error': 'Insufficient permissions to view tenant administrators'
                }, status=status.HTTP_403_FORBIDDEN)
        except UserProfile.DoesNotExist:
            return Response({
                'error': 'User profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        tenant_admins = self.get_queryset().filter(role__in=['tenant_admin', 'super_admin'])
        serializer = self.get_serializer(tenant_admins, many=True)
        
        return Response({
            'tenant_admins': serializer.data,
            'count': tenant_admins.count(),
            'tenant_name': request.tenant.name if request.tenant else 'Unknown'
        })
    
    @action(detail=True, methods=['post'])
    def promote_to_admin(self, request, pk=None):
        """
        Promote a user to tenant admin role.
        """
        user_profile = self.get_object()
        
        # Check permissions
        try:
            current_user_profile = UserProfile.objects.get(user=request.user)
            if current_user_profile.role not in ['super_admin', 'tenant_admin']:
                return Response({
                    'error': 'Insufficient permissions to promote users to admin'
                }, status=status.HTTP_403_FORBIDDEN)
        except UserProfile.DoesNotExist:
            return Response({
                'error': 'Current user profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Prevent self-promotion unless super admin
        if (user_profile.user == request.user and 
            current_user_profile.role != 'super_admin'):
            return Response({
                'error': 'Cannot promote yourself to admin'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        old_role = user_profile.role
        user_profile.role = 'tenant_admin'
        user_profile.updated_by = request.user
        user_profile.updated_at = timezone.now()
        user_profile.save()
        
        return Response({
            'message': f'User promoted from {old_role} to tenant_admin',
            'user_id': user_profile.user.id,
            'username': user_profile.user.username,
            'old_role': old_role,
            'new_role': 'tenant_admin'
        })
