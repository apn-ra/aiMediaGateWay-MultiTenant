"""
Permission classes for multi-tenant API access control in aiMediaGateway

This module provides permission classes that enforce tenant isolation
and role-based access control for the Django REST Framework API.
"""

import logging
from typing import Dict, Set, List, Optional
from django.core.cache import cache
from rest_framework import permissions
from django.contrib.auth.models import User
from core.models import Tenant, UserProfile

logger = logging.getLogger(__name__)


# Permission Matrix - Defines what each role can do for each model/action
PERMISSION_MATRIX = {
    'super_admin': {
        'Tenant': {'create', 'read', 'update', 'delete', 'manage'},
        'UserProfile': {'create', 'read', 'update', 'delete', 'manage'},
        'CallSession': {'create', 'read', 'update', 'delete', 'control', 'monitor'},
        'AudioRecording': {'create', 'read', 'update', 'delete', 'download', 'transcribe'},
        'SystemConfiguration': {'create', 'read', 'update', 'delete', 'manage'},
        'AudioTranscription': {'create', 'read', 'update', 'delete'},
    },
    'tenant_admin': {
        'Tenant': {'read', 'update'},  # Own tenant only
        'UserProfile': {'create', 'read', 'update', 'delete'},  # Own tenant users only
        'CallSession': {'create', 'read', 'update', 'delete', 'control', 'monitor'},
        'AudioRecording': {'create', 'read', 'update', 'delete', 'download', 'transcribe'},
        'SystemConfiguration': {'read', 'update'},  # Tenant-specific config only
        'AudioTranscription': {'create', 'read', 'update', 'delete'},
    },
    'operator': {
        'UserProfile': {'read'},  # Own profile and team members
        'CallSession': {'create', 'read', 'update', 'control', 'monitor'},
        'AudioRecording': {'create', 'read', 'download'},
        'SystemConfiguration': {'read'},  # Read-only limited config
        'AudioTranscription': {'read'},
    },
    'viewer': {
        'UserProfile': {'read'},  # Own profile only
        'CallSession': {'read', 'monitor'},
        'AudioRecording': {'read'},
        'SystemConfiguration': {'read'},  # Read-only limited config
        'AudioTranscription': {'read'},
    },
}

# Field-level permissions for different roles
FIELD_LEVEL_PERMISSIONS = {
    'UserProfile': {
        'super_admin': {'all_fields'},
        'tenant_admin': {'all_fields'},
        'operator': {'username', 'email', 'role', 'created_at', 'last_login'},
        'viewer': {'username', 'role', 'created_at'},
    },
    'Tenant': {
        'super_admin': {'all_fields'},
        'tenant_admin': {'name', 'slug', 'description', 'asterisk_host', 'ami_port', 'ari_port'},
        'operator': {'name', 'slug'},
        'viewer': {'name', 'slug'},
    },
    'CallSession': {
        'super_admin': {'all_fields'},
        'tenant_admin': {'all_fields'},
        'operator': {'all_fields'},
        'viewer': {'session_id', 'caller_id', 'dialed_number', 'status', 'start_time', 'end_time', 'duration'},
    },
    'SystemConfiguration': {
        'super_admin': {'all_fields'},
        'tenant_admin': {'key', 'value', 'description'},
        'operator': {'key', 'value'},
        'viewer': {'key'},
    },
}

# Role inheritance hierarchy - higher roles inherit permissions from lower roles
ROLE_HIERARCHY = {
    'super_admin': ['tenant_admin', 'operator', 'viewer'],
    'tenant_admin': ['operator', 'viewer'],
    'operator': ['viewer'],
    'viewer': [],
}


def get_user_effective_permissions(user_role: str, model_name: str) -> Set[str]:
    """
    Get effective permissions for a user role on a model, including inherited permissions.
    
    Args:
        user_role: The user's role (e.g., 'tenant_admin', 'operator')
        model_name: The model name (e.g., 'CallSession', 'AudioRecording')
    
    Returns:
        Set of permission strings (e.g., {'read', 'update', 'create'})
    """
    cache_key = f"permissions:{user_role}:{model_name}"
    cached_permissions = cache.get(cache_key)
    if cached_permissions is not None:
        return cached_permissions
    
    permissions = set()
    
    # Add direct permissions for this role
    if user_role in PERMISSION_MATRIX:
        if model_name in PERMISSION_MATRIX[user_role]:
            permissions.update(PERMISSION_MATRIX[user_role][model_name])
    
    # Add inherited permissions from lower roles
    if user_role in ROLE_HIERARCHY:
        for inherited_role in ROLE_HIERARCHY[user_role]:
            if inherited_role in PERMISSION_MATRIX:
                if model_name in PERMISSION_MATRIX[inherited_role]:
                    permissions.update(PERMISSION_MATRIX[inherited_role][model_name])
    
    # Cache for 5 minutes
    cache.set(cache_key, permissions, 300)
    return permissions


def check_permission(user_role: str, model_name: str, action: str) -> bool:
    """
    Check if a user role has permission to perform an action on a model.
    
    Args:
        user_role: The user's role
        model_name: The model name
        action: The action to check (e.g., 'read', 'update', 'delete')
    
    Returns:
        bool: True if permission is granted
    """
    effective_permissions = get_user_effective_permissions(user_role, model_name)
    return action in effective_permissions


def get_allowed_fields(user_role: str, model_name: str) -> Set[str]:
    """
    Get fields that a user role is allowed to access for a model.
    
    Args:
        user_role: The user's role
        model_name: The model name
    
    Returns:
        Set of field names, or {'all_fields'} if all fields are allowed
    """
    cache_key = f"fields:{user_role}:{model_name}"
    cached_fields = cache.get(cache_key)
    if cached_fields is not None:
        return cached_fields
    
    allowed_fields = set()
    
    # Check direct field permissions
    if model_name in FIELD_LEVEL_PERMISSIONS:
        if user_role in FIELD_LEVEL_PERMISSIONS[model_name]:
            allowed_fields.update(FIELD_LEVEL_PERMISSIONS[model_name][user_role])
    
    # Check inherited field permissions (use highest permission level found)
    if user_role in ROLE_HIERARCHY:
        for inherited_role in ROLE_HIERARCHY[user_role]:
            if (model_name in FIELD_LEVEL_PERMISSIONS and 
                inherited_role in FIELD_LEVEL_PERMISSIONS[model_name]):
                inherited_fields = FIELD_LEVEL_PERMISSIONS[model_name][inherited_role]
                if 'all_fields' in inherited_fields:
                    allowed_fields = {'all_fields'}
                    break
                allowed_fields.update(inherited_fields)
    
    # If no specific permissions found, deny all
    if not allowed_fields:
        allowed_fields = set()
    
    # Cache for 5 minutes
    cache.set(cache_key, allowed_fields, 300)
    return allowed_fields


def filter_serializer_fields(serializer_data: dict, user_role: str, model_name: str) -> dict:
    """
    Filter serializer data based on user's field-level permissions.
    
    Args:
        serializer_data: The serializer data dictionary
        user_role: The user's role
        model_name: The model name
    
    Returns:
        Filtered dictionary with only allowed fields
    """
    allowed_fields = get_allowed_fields(user_role, model_name)
    
    # If all fields are allowed, return original data
    if 'all_fields' in allowed_fields:
        return serializer_data
    
    # Filter to only allowed fields
    filtered_data = {}
    for field_name, field_value in serializer_data.items():
        if field_name in allowed_fields:
            filtered_data[field_name] = field_value
    
    return filtered_data


class DynamicPermissionMixin:
    """
    Mixin that provides dynamic permission checking using the permission matrix.
    """
    
    def check_action_permission(self, request, model_name: str, action: str) -> bool:
        """
        Check if the current user has permission for a specific action.
        
        Args:
            request: Django HttpRequest object
            model_name: The model name
            action: The action to check
        
        Returns:
            bool: True if permission is granted
        """
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return False
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            return check_permission(user_profile.role, model_name, action)
        except UserProfile.DoesNotExist:
            return False
    
    def get_user_role(self, request) -> Optional[str]:
        """
        Get the user's role from their profile.
        
        Args:
            request: Django HttpRequest object
        
        Returns:
            User's role string or None
        """
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return None
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            return user_profile.role
        except UserProfile.DoesNotExist:
            return None


class TenantPermission(permissions.BasePermission, DynamicPermissionMixin):
    """
    Base permission class for tenant-scoped access control.
    
    This class ensures that users can only access resources within their assigned tenant
    and provides common functionality for tenant-based permissions.
    """
    
    def has_permission(self, request, view):
        """
        Check if the user has permission to access the view within tenant context.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # Ensure user is authenticated
        if not request.user or not request.user.is_authenticated:
            return False

        if request.user.is_superuser or request.user.is_staff:
            return True

        # Ensure tenant context is available (set by TenantAPIMiddleware)
        if not hasattr(request, 'tenant') or not request.tenant:
            logger.warning(f"No tenant context for user {request.user.username}")
            return False
        
        # Check if user belongs to the tenant
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            if user_profile.tenant != request.tenant:
                logger.warning(f"User {request.user.username} attempted access to tenant {request.tenant.slug} but belongs to {user_profile.tenant.slug}")
                return False
                
        except UserProfile.DoesNotExist:
            logger.warning(f"No user profile found for user {request.user.username}")
            return False
        
        return True
    
    def has_object_permission(self, request, view, obj):
        """
        Check if the user has permission to access a specific object within tenant context.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            obj: Model instance being accessed
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not self.has_permission(request, view):
            return False
        
        # Check if object belongs to the tenant (if it has a tenant field)
        if hasattr(obj, 'tenant'):
            return obj.tenant == request.tenant
        
        # For objects without direct tenant relationship, check via related fields
        if hasattr(obj, 'user') and hasattr(obj.user, 'userprofile'):
            return obj.user.userprofile.tenant == request.tenant
        
        # If no tenant relationship can be established, allow access
        # (individual permission classes should implement stricter checks)
        return True


class TenantAdminPermission(TenantPermission):
    """
    Permission class for tenant administrators.
    
    Allows full access to all resources within the tenant for users with admin role.
    """
    
    def has_permission(self, request, view):
        """
        Check if the user has tenant admin permission.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not super().has_permission(request, view):
            return False
        
        # Check if user has admin role
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            if user_profile.role not in ['admin', 'tenant_admin', 'super_admin']:
                logger.debug(f"User {request.user.username} denied admin access - role: {user_profile.role}")
                return False
                
        except UserProfile.DoesNotExist:
            return False
        
        return True
    
    def has_object_permission(self, request, view, obj):
        """
        Check if the user has admin permission for a specific object.
        
        Tenant admins have full access to all objects within their tenant.
        """
        return super().has_object_permission(request, view, obj)


class CallOperatorPermission(TenantPermission):
    """
    Permission class for call operators.
    
    Allows access to call-related resources with read/write permissions for call management.
    """
    
    def has_permission(self, request, view):
        """
        Check if the user has call operator permission.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not super().has_permission(request, view):
            return False
        
        # Check if user has operator or admin role
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            if user_profile.role not in ['admin', 'tenant_admin', 'operator', 'call_operator']:
                logger.debug(f"User {request.user.username} denied operator access - role: {user_profile.role}")
                return False
                
        except UserProfile.DoesNotExist:
            return False
        
        return True
    
    def has_object_permission(self, request, view, obj):
        """
        Check if the user has operator permission for a specific object.
        
        Call operators can manage call sessions and audio recordings within their tenant.
        """
        if not super().has_object_permission(request, view, obj):
            return False
        
        # Allow access to call-related objects
        allowed_models = ['CallSession', 'AudioRecording', 'AudioTranscription']
        
        if obj.__class__.__name__ in allowed_models:
            return True
        
        # For safe methods (GET, HEAD, OPTIONS), allow broader access
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Deny access to sensitive configuration objects for non-admin users
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            if user_profile.role in ['admin', 'tenant_admin']:
                return True
        except UserProfile.DoesNotExist:
            pass
        
        return False


class ViewOnlyPermission(TenantPermission):
    """
    Permission class for view-only users.
    
    Allows read-only access to resources within the tenant.
    """
    
    def has_permission(self, request, view):
        """
        Check if the user has view-only permission.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not super().has_permission(request, view):
            return False
        
        # Allow only safe methods (GET, HEAD, OPTIONS)
        if request.method not in permissions.SAFE_METHODS:
            logger.debug(f"User {request.user.username} denied write access - view-only permission")
            return False
        
        # Check if user has viewer or higher role
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            if user_profile.role not in ['admin', 'tenant_admin', 'operator', 'call_operator', 'viewer', 'view_only']:
                logger.debug(f"User {request.user.username} denied viewer access - role: {user_profile.role}")
                return False
                
        except UserProfile.DoesNotExist:
            return False
        
        return True
    
    def has_object_permission(self, request, view, obj):
        """
        Check if the user has view-only permission for a specific object.
        
        View-only users can read all objects within their tenant but cannot modify them.
        """
        if not super().has_object_permission(request, view, obj):
            return False
        
        # Only allow safe methods
        if request.method not in permissions.SAFE_METHODS:
            return False
        
        return True


class TenantResourcePermission(TenantPermission):
    """
    Permission class for general tenant resource access.
    
    Provides role-based permissions for different types of operations within a tenant.
    """
    
    def has_permission(self, request, view):
        """
        Check permission based on user role and request method.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not super().has_permission(request, view):
            return False
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            user_role = user_profile.role
            
            # Admin users have full access
            if user_role in ['admin', 'tenant_admin', 'super_admin']:
                return True
            
            # Operators can read and write call-related data
            if user_role in ['operator', 'call_operator']:
                return True
            
            # Viewers can only read
            if user_role in ['viewer', 'view_only']:
                return request.method in permissions.SAFE_METHODS
            
            # Unknown role, deny access
            logger.warning(f"Unknown user role: {user_role} for user {request.user.username}")
            return False
                
        except UserProfile.DoesNotExist:
            logger.warning(f"No user profile found for user {request.user.username}")
            return False
    
    def has_object_permission(self, request, view, obj):
        """
        Check object-level permission based on user role and object type.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            obj: Model instance being accessed
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        if not super().has_object_permission(request, view, obj):
            return False
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            user_role = user_profile.role
            
            # Admin users have full access to all objects
            if user_role in ['admin', 'tenant_admin', 'super_admin']:
                return True
            
            # For sensitive configuration objects, only admins have access
            if obj.__class__.__name__ in ['SystemConfiguration', 'Tenant']:
                return user_role in ['admin', 'tenant_admin', 'super_admin']
            
            # For user-related objects, allow access to own data or admin access
            if obj.__class__.__name__ in ['User', 'UserProfile']:
                if obj == request.user or (hasattr(obj, 'user') and obj.user == request.user):
                    return True
                return user_role in ['admin', 'tenant_admin', 'super_admin']
            
            # For other objects, apply role-based permissions
            if user_role in ['operator', 'call_operator']:
                # Operators can manage call-related data
                if obj.__class__.__name__ in ['CallSession', 'AudioRecording', 'AudioTranscription']:
                    return True
                # Read-only access to other objects
                return request.method in permissions.SAFE_METHODS
            
            if user_role in ['viewer', 'view_only']:
                # Viewers can only read
                return request.method in permissions.SAFE_METHODS
            
            return False
                
        except UserProfile.DoesNotExist:
            return False


class MatrixBasedPermission(TenantPermission):
    """
    Enhanced permission class that uses the permission matrix system for comprehensive
    role-based access control with field-level permissions and role inheritance.
    """
    
    def has_permission(self, request, view):
        """
        Check permission using the permission matrix system.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not super().has_permission(request, view):
            return False
        
        # Get model name from view
        model_name = self._get_model_name(view)
        if not model_name:
            logger.warning(f"Could not determine model name for view {view.__class__.__name__}")
            return False
        
        # Get action from request method
        action = self._get_action_from_method(request.method)
        
        # Check permission using matrix
        user_role = self.get_user_role(request)
        if not user_role:
            return False
        
        has_permission = self.check_action_permission(request, model_name, action)
        
        if not has_permission:
            logger.debug(f"User {request.user.username} with role {user_role} denied {action} access to {model_name}")
        
        return has_permission
    
    def has_object_permission(self, request, view, obj):
        """
        Check object-level permission using the permission matrix system.
        
        Args:
            request: Django HttpRequest object with tenant context
            view: DRF View object
            obj: Model instance being accessed
            
        Returns:
            bool: True if permission granted, False otherwise
        """
        # First check basic tenant permission
        if not super().has_object_permission(request, view, obj):
            return False
        
        # Get model name from object
        model_name = obj.__class__.__name__
        
        # Get action from request method
        action = self._get_action_from_method(request.method)
        
        # Check permission using matrix
        user_role = self.get_user_role(request)
        if not user_role:
            return False
        
        has_permission = check_permission(user_role, model_name, action)
        
        if not has_permission:
            logger.debug(f"User {request.user.username} with role {user_role} denied {action} access to {model_name} object {obj.pk}")
            return False
        
        # Additional object-specific checks
        return self._check_object_specific_permissions(request, obj, user_role, action)
    
    def _get_model_name(self, view) -> Optional[str]:
        """
        Extract model name from view.
        
        Args:
            view: DRF View object
            
        Returns:
            Model name string or None
        """
        if hasattr(view, 'queryset') and view.queryset is not None:
            return view.queryset.model.__name__
        elif hasattr(view, 'serializer_class') and view.serializer_class:
            if hasattr(view.serializer_class.Meta, 'model'):
                return view.serializer_class.Meta.model.__name__
        return None
    
    def _get_action_from_method(self, method: str) -> str:
        """
        Map HTTP method to permission action.
        
        Args:
            method: HTTP method string
            
        Returns:
            Permission action string
        """
        method_action_map = {
            'GET': 'read',
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'update',
            'DELETE': 'delete',
            'HEAD': 'read',
            'OPTIONS': 'read',
        }
        return method_action_map.get(method.upper(), 'read')
    
    def _check_object_specific_permissions(self, request, obj, user_role: str, action: str) -> bool:
        """
        Perform additional object-specific permission checks.
        
        Args:
            request: Django HttpRequest object
            obj: Model instance being accessed
            user_role: User's role
            action: Action being performed
            
        Returns:
            bool: True if permission granted
        """
        model_name = obj.__class__.__name__
        
        # For user-related objects, allow access to own data
        if model_name in ['User', 'UserProfile']:
            if obj == request.user or (hasattr(obj, 'user') and obj.user == request.user):
                # Users can read/update their own data if they have basic read permission
                if action in ['read', 'update'] and check_permission(user_role, model_name, 'read'):
                    return True
            
            # Admin access for other users' data
            return user_role in ['super_admin', 'tenant_admin']
        
        # For sensitive configuration, require admin access
        if model_name in ['SystemConfiguration'] and action in ['update', 'delete', 'create']:
            return user_role in ['super_admin', 'tenant_admin']
        
        # For tenant objects, only allow access to own tenant
        if model_name == 'Tenant':
            if hasattr(request, 'tenant') and obj == request.tenant:
                return True
            return user_role == 'super_admin'
        
        return True


class FieldLevelPermissionMixin:
    """
    Mixin that provides field-level permission filtering for serializers.
    """
    
    def to_representation(self, instance):
        """
        Filter fields based on user permissions before serialization.
        """
        # Get the full representation first
        data = super().to_representation(instance)
        
        # Get user role from request context
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return data
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            user_role = user_profile.role
            model_name = instance.__class__.__name__
            
            # Filter fields based on permissions
            filtered_data = filter_serializer_fields(data, user_role, model_name)
            return filtered_data
            
        except UserProfile.DoesNotExist:
            # If no profile found, return empty data for security
            return {}
    
    def get_fields(self):
        """
        Dynamically adjust fields based on user permissions.
        """
        fields = super().get_fields()
        request = self.context.get('request')
        
        if not request or not request.user or not request.user.is_authenticated:
            return fields
        
        try:
            user_profile = UserProfile.objects.get(user=request.user)
            user_role = user_profile.role
            
            # Get model name from Meta
            if hasattr(self.Meta, 'model'):
                model_name = self.Meta.model.__name__
                allowed_fields = get_allowed_fields(user_role, model_name)
                
                if 'all_fields' not in allowed_fields and allowed_fields:
                    # Remove fields that are not allowed
                    fields_to_remove = []
                    for field_name in fields.keys():
                        if field_name not in allowed_fields:
                            fields_to_remove.append(field_name)
                    
                    for field_name in fields_to_remove:
                        fields.pop(field_name, None)
        
        except UserProfile.DoesNotExist:
            pass
        
        return fields
