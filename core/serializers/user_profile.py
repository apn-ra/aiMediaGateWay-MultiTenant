"""
UserProfile serializers for aiMediaGateway

This module contains serializers for the UserProfile model with role-based
field access and tenant integration.
"""

from rest_framework import serializers
from django.contrib.auth.models import User
from core.models import UserProfile, Tenant


class UserProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for UserProfile model with role-based fields.
    
    Provides different field access based on user roles and ensures
    tenant isolation for user management.
    """
    
    # Include user fields for convenience
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    is_active = serializers.BooleanField(source='user.is_active', read_only=True)
    date_joined = serializers.DateTimeField(source='user.date_joined', read_only=True)
    last_login = serializers.DateTimeField(source='user.last_login', read_only=True)
    
    # Read-only fields
    id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    
    # Tenant information
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    
    # Computed fields
    full_name = serializers.SerializerMethodField()
    permissions_list = serializers.SerializerMethodField()
    
    class Meta:
        model = UserProfile
        fields = [
            'id',
            'username',
            'email',
            'first_name',
            'last_name',
            'full_name',
            'is_active',
            'date_joined',
            'last_login',
            'role',
            'phone_number',
            'department',
            'tenant',
            'tenant_name',
            'tenant_slug',
            'permissions',
            'permissions_list',
            'configuration',
            'created_at',
            'updated_at',
        ]
        
        extra_kwargs = {
            'tenant': {'read_only': True},  # Tenant is set by middleware/context
        }
    
    def get_full_name(self, obj):
        """
        Get the user's full name.
        
        Args:
            obj: UserProfile instance
            
        Returns:
            str: User's full name or username if no first/last name
        """
        if obj.user.first_name and obj.user.last_name:
            return f"{obj.user.first_name} {obj.user.last_name}"
        return obj.user.username
    
    def get_permissions_list(self, obj):
        """
        Get a list of permissions for this user based on their role.
        
        Args:
            obj: UserProfile instance
            
        Returns:
            list: List of permission strings
        """
        role_permissions = {
            'admin': [
                'view_all_sessions',
                'manage_all_sessions',
                'view_all_recordings',
                'manage_all_recordings',
                'manage_users',
                'manage_tenant_config',
                'view_system_config',
                'manage_system_config',
            ],
            'tenant_admin': [
                'view_all_sessions',
                'manage_all_sessions',
                'view_all_recordings',
                'manage_all_recordings',
                'manage_users',
                'manage_tenant_config',
                'view_system_config',
            ],
            'operator': [
                'view_own_sessions',
                'manage_own_sessions',
                'view_all_sessions',
                'view_all_recordings',
                'manage_call_sessions',
            ],
            'call_operator': [
                'view_own_sessions',
                'manage_own_sessions',
                'view_all_sessions',
                'view_all_recordings',
                'manage_call_sessions',
            ],
            'viewer': [
                'view_own_sessions',
                'view_all_sessions',
                'view_all_recordings',
            ],
            'view_only': [
                'view_own_sessions',
                'view_all_sessions',
                'view_all_recordings',
            ],
        }
        
        return role_permissions.get(obj.role, [])
    
    def validate_role(self, value):
        """
        Validate that the role is allowed and user has permission to assign it.
        
        Args:
            value: The role value to validate
            
        Returns:
            str: The validated role value
            
        Raises:
            ValidationError: If role assignment is not allowed
        """
        request = self.context.get('request')
        
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            try:
                current_user_profile = UserProfile.objects.get(user=request.user)
                current_role = current_user_profile.role
                
                # Only admins can assign admin roles
                if value in ['admin', 'tenant_admin'] and current_role not in ['admin']:
                    raise serializers.ValidationError(
                        "You don't have permission to assign admin roles."
                    )
                
                # Tenant admins can assign non-admin roles
                if current_role == 'tenant_admin' and value == 'admin':
                    raise serializers.ValidationError(
                        "Tenant admins cannot assign system admin role."
                    )
                
            except UserProfile.DoesNotExist:
                raise serializers.ValidationError(
                    "Current user profile not found."
                )
        
        return value
    
    def validate_phone_number(self, value):
        """
        Validate phone number format.
        
        Args:
            value: The phone number to validate
            
        Returns:
            str: The validated phone number
            
        Raises:
            ValidationError: If phone number format is invalid
        """
        if value:
            import re
            # Basic phone number validation (international format)
            if not re.match(r'^\+?[1-9]\d{1,14}$', value.replace(' ', '').replace('-', '')):
                raise serializers.ValidationError(
                    "Phone number must be in valid international format (e.g., +1234567890)."
                )
        
        return value
    
    def validate_configuration(self, value):
        """
        Validate user configuration JSON structure.
        
        Args:
            value: The configuration dictionary to validate
            
        Returns:
            dict: The validated configuration
            
        Raises:
            ValidationError: If configuration is invalid
        """
        if value is not None:
            # Ensure it's a dictionary
            if not isinstance(value, dict):
                raise serializers.ValidationError("Configuration must be a valid JSON object.")
            
            # Validate notification preferences if present
            if 'notifications' in value:
                notifications = value['notifications']
                if not isinstance(notifications, dict):
                    raise serializers.ValidationError("Notifications configuration must be an object.")
                
                # Validate notification settings
                valid_notification_types = ['email', 'sms', 'push', 'desktop']
                for notif_type, enabled in notifications.items():
                    if notif_type not in valid_notification_types:
                        raise serializers.ValidationError(
                            f"Invalid notification type: {notif_type}. "
                            f"Valid types are: {', '.join(valid_notification_types)}"
                        )
                    if not isinstance(enabled, bool):
                        raise serializers.ValidationError(
                            f"Notification setting for {notif_type} must be boolean."
                        )
        
        return value
    
    def to_representation(self, instance):
        """
        Customize the serialized representation based on user permissions.
        
        Args:
            instance: UserProfile instance
            
        Returns:
            dict: Customized representation
        """
        data = super().to_representation(instance)
        request = self.context.get('request')
        
        # If current user is not admin, hide sensitive information
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            try:
                current_user_profile = UserProfile.objects.get(user=request.user)
                
                # Non-admin users can only see limited info about other users
                if (current_user_profile.role not in ['admin', 'tenant_admin'] and 
                    instance.user != request.user):
                    
                    # Remove sensitive fields
                    sensitive_fields = ['email', 'phone_number', 'permissions', 'configuration']
                    for field in sensitive_fields:
                        data.pop(field, None)
                
            except UserProfile.DoesNotExist:
                pass
        
        return data


class UserPermissionSerializer(serializers.Serializer):
    """
    Serializer for user role management and permission assignments.
    
    Used for viewing and updating user permissions and roles.
    """
    
    user_id = serializers.IntegerField()
    username = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    current_role = serializers.CharField(read_only=True)
    new_role = serializers.ChoiceField(
        choices=UserProfile.ROLE_CHOICES,
        required=False,
        help_text="New role to assign to the user"
    )
    permissions = serializers.JSONField(read_only=True)
    
    def validate_user_id(self, value):
        """
        Validate that the user exists and belongs to the current tenant.
        
        Args:
            value: User ID to validate
            
        Returns:
            int: Validated user ID
            
        Raises:
            ValidationError: If user is invalid
        """
        request = self.context.get('request')
        
        try:
            user = User.objects.get(id=value)
            user_profile = UserProfile.objects.get(user=user)
            
            # Ensure user belongs to the same tenant
            if request and hasattr(request, 'tenant'):
                if user_profile.tenant != request.tenant:
                    raise serializers.ValidationError(
                        "User does not belong to the current tenant."
                    )
            
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found.")
        except UserProfile.DoesNotExist:
            raise serializers.ValidationError("User profile not found.")
        
        return value
    
    def validate_new_role(self, value):
        """
        Validate that the current user can assign the new role.
        
        Args:
            value: New role to validate
            
        Returns:
            str: Validated role
            
        Raises:
            ValidationError: If role assignment is not allowed
        """
        request = self.context.get('request')
        
        if value and request and hasattr(request, 'user') and request.user.is_authenticated:
            try:
                current_user_profile = UserProfile.objects.get(user=request.user)
                current_role = current_user_profile.role
                
                # Only admins can assign admin roles
                if value in ['admin', 'tenant_admin'] and current_role not in ['admin']:
                    raise serializers.ValidationError(
                        "You don't have permission to assign admin roles."
                    )
                
                # Tenant admins can assign non-admin roles
                if current_role == 'tenant_admin' and value == 'admin':
                    raise serializers.ValidationError(
                        "Tenant admins cannot assign system admin role."
                    )
                
            except UserProfile.DoesNotExist:
                raise serializers.ValidationError(
                    "Current user profile not found."
                )
        
        return value
