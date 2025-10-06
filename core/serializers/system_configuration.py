"""
SystemConfiguration serializers for aiMediaGateway

This module contains serializers for the SystemConfiguration model with
type validation for different configuration value types.
"""

from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
import json
from core.models import SystemConfiguration


class SystemConfigurationSerializer(serializers.ModelSerializer):
    """
    Serializer for SystemConfiguration model with type validation.
    
    Provides type-safe configuration management with validation
    for different value types (string, integer, float, boolean, JSON).
    """
    
    # Read-only fields
    id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    
    # Computed fields
    typed_value = serializers.SerializerMethodField()
    is_system_default = serializers.SerializerMethodField()
    
    class Meta:
        model = SystemConfiguration
        fields = [
            'id',
            'tenant',
            'key',
            'value',
            'typed_value',
            'value_type',
            'description',
            'is_sensitive',
            'is_system_default',
            'category',
            'created_at',
            'updated_at',
        ]
        
        extra_kwargs = {
            'tenant': {'allow_null': True},  # System-wide configs have null tenant
        }
    
    def get_typed_value(self, obj):
        """
        Get the configuration value converted to its proper type.
        
        Args:
            obj: SystemConfiguration instance
            
        Returns:
            Any: Value converted to proper Python type
        """
        try:
            return obj.get_typed_value()
        except (ValueError, TypeError, json.JSONDecodeError):
            return obj.value  # Return raw value if conversion fails
    
    def get_is_system_default(self, obj):
        """
        Check if this is a system-wide default configuration.
        
        Args:
            obj: SystemConfiguration instance
            
        Returns:
            bool: True if system-wide configuration
        """
        return obj.tenant is None
    
    def validate_key(self, value):
        """
        Validate configuration key format.
        
        Args:
            value: Configuration key to validate
            
        Returns:
            str: Validated key
        """
        if value:
            # Key should be alphanumeric with underscores and dots
            import re
            if not re.match(r'^[a-zA-Z][a-zA-Z0-9_\.]*$', value):
                raise serializers.ValidationError(
                    "Configuration key must start with a letter and contain only "
                    "letters, numbers, underscores, and dots."
                )
            
            # Check for reserved keys
            reserved_keys = [
                'system.version',
                'system.secret_key',
                'database.password',
                'redis.password',
            ]
            
            if value in reserved_keys:
                raise serializers.ValidationError(f"'{value}' is a reserved configuration key.")
        
        return value
    
    def validate_value_type(self, value):
        """
        Validate value type is supported.
        
        Args:
            value: Value type to validate
            
        Returns:
            str: Validated value type
        """
        valid_types = ['string', 'integer', 'float', 'boolean', 'json']
        
        if value not in valid_types:
            raise serializers.ValidationError(
                f"Invalid value type. Must be one of: {', '.join(valid_types)}"
            )
        
        return value
    
    def validate_category(self, value):
        """
        Validate configuration category.
        
        Args:
            value: Category to validate
            
        Returns:
            str: Validated category
        """
        if value:
            valid_categories = [
                'system',
                'database',
                'redis',
                'asterisk',
                'audio',
                'transcription',
                'security',
                'performance',
                'logging',
                'notifications',
                'integration',
                'ui',
                'api',
            ]
            
            if value not in valid_categories:
                raise serializers.ValidationError(
                    f"Invalid category. Valid categories: {', '.join(valid_categories)}"
                )
        
        return value
    
    def validate(self, data):
        """
        Validate the entire configuration object.
        
        Args:
            data: Complete validated data
            
        Returns:
            dict: Validated data
        """
        value = data.get('value')
        value_type = data.get('value_type')
        
        if value is not None and value_type:
            # Validate value matches the specified type
            try:
                self._validate_typed_value(value, value_type)
            except ValueError as e:
                raise serializers.ValidationError({
                    'value': f"Value does not match type '{value_type}': {str(e)}"
                })
        
        # Check for duplicate keys within tenant scope
        key = data.get('key')
        tenant = data.get('tenant')
        
        if key:
            # Build query for duplicate check
            duplicate_query = SystemConfiguration.objects.filter(key=key)
            
            if tenant:
                duplicate_query = duplicate_query.filter(tenant=tenant)
            else:
                duplicate_query = duplicate_query.filter(tenant__isnull=True)
            
            # Exclude current instance if updating
            if self.instance:
                duplicate_query = duplicate_query.exclude(id=self.instance.id)
            
            if duplicate_query.exists():
                scope = f"tenant '{tenant.name}'" if tenant else "system-wide"
                raise serializers.ValidationError({
                    'key': f"Configuration key '{key}' already exists for {scope} scope."
                })
        
        return data
    
    def _validate_typed_value(self, value, value_type):
        """
        Validate that a value can be converted to the specified type.
        
        Args:
            value: Value to validate
            value_type: Expected type
            
        Raises:
            ValueError: If value cannot be converted to type
        """
        if value_type == 'string':
            # String values are always valid
            pass
        
        elif value_type == 'integer':
            try:
                int(value)
            except (ValueError, TypeError):
                raise ValueError(f"Cannot convert '{value}' to integer")
        
        elif value_type == 'float':
            try:
                float(value)
            except (ValueError, TypeError):
                raise ValueError(f"Cannot convert '{value}' to float")
        
        elif value_type == 'boolean':
            if isinstance(value, str):
                if value.lower() not in ['true', 'false', '1', '0', 'yes', 'no']:
                    raise ValueError(f"Cannot convert '{value}' to boolean")
            elif not isinstance(value, bool):
                try:
                    bool(value)
                except (ValueError, TypeError):
                    raise ValueError(f"Cannot convert '{value}' to boolean")
        
        elif value_type == 'json':
            if isinstance(value, str):
                try:
                    json.loads(value)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON: {str(e)}")
            elif not isinstance(value, (dict, list)):
                raise ValueError("JSON value must be a valid JSON string, dict, or list")
    
    def to_representation(self, instance):
        """
        Customize representation based on user permissions.
        
        Args:
            instance: SystemConfiguration instance
            
        Returns:
            dict: Customized representation
        """
        data = super().to_representation(instance)
        request = self.context.get('request')
        
        # Hide sensitive configuration values from non-admin users
        if instance.is_sensitive and request and hasattr(request, 'user'):
            try:
                from core.models import UserProfile
                user_profile = UserProfile.objects.get(user=request.user)
                
                # Only system admins can see sensitive values
                if user_profile.role != 'admin':
                    data['value'] = '***HIDDEN***'
                    data['typed_value'] = '***HIDDEN***'
                    
            except UserProfile.DoesNotExist:
                data['value'] = '***HIDDEN***'
                data['typed_value'] = '***HIDDEN***'
        
        return data
    
    def create(self, validated_data):
        """
        Create a new system configuration.
        
        Args:
            validated_data: Validated data from serializer
            
        Returns:
            SystemConfiguration: Created configuration instance
        """
        # Set tenant from request context if not provided
        request = self.context.get('request')
        if not validated_data.get('tenant') and request and hasattr(request, 'tenant'):
            validated_data['tenant'] = request.tenant
        
        return super().create(validated_data)


class TenantStatsSerializer(serializers.Serializer):
    """
    Serializer for aggregated tenant statistics.
    
    Provides statistical data about tenant usage and performance.
    """
    
    tenant_id = serializers.IntegerField()
    tenant_name = serializers.CharField()
    tenant_slug = serializers.CharField()
    
    # User statistics
    total_users = serializers.IntegerField()
    active_users = serializers.IntegerField()
    admin_users = serializers.IntegerField()
    
    # Call session statistics
    total_sessions = serializers.IntegerField()
    active_sessions = serializers.IntegerField()
    completed_sessions = serializers.IntegerField()
    failed_sessions = serializers.IntegerField()
    
    # Audio recording statistics
    total_recordings = serializers.IntegerField()
    total_recording_size_mb = serializers.FloatField()
    transcribed_recordings = serializers.IntegerField()
    
    # Time period statistics
    sessions_today = serializers.IntegerField()
    sessions_this_week = serializers.IntegerField()
    sessions_this_month = serializers.IntegerField()
    
    # Quality metrics
    average_call_duration = serializers.FloatField(allow_null=True)
    average_recording_size_mb = serializers.FloatField(allow_null=True)
    transcription_success_rate = serializers.FloatField(allow_null=True)
    
    # System usage
    storage_used_mb = serializers.FloatField()
    bandwidth_used_mb = serializers.FloatField(allow_null=True)
    
    # Timestamps
    last_activity = serializers.DateTimeField(allow_null=True)
    stats_generated_at = serializers.DateTimeField()
    
    def validate_transcription_success_rate(self, value):
        """
        Validate transcription success rate is a valid percentage.
        
        Args:
            value: Success rate to validate
            
        Returns:
            float: Validated success rate
        """
        if value is not None:
            if not (0.0 <= value <= 100.0):
                raise serializers.ValidationError(
                    "Transcription success rate must be between 0.0 and 100.0"
                )
        return value
    
    def validate_average_call_duration(self, value):
        """
        Validate average call duration is positive.
        
        Args:
            value: Duration to validate
            
        Returns:
            float: Validated duration
        """
        if value is not None and value < 0:
            raise serializers.ValidationError(
                "Average call duration cannot be negative"
            )
        return value
