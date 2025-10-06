"""
Tenant serializers for aiMediaGateway

This module contains serializers for the Tenant model with multi-tenant
configuration management capabilities.
"""

from rest_framework import serializers
from core.models import Tenant


class TenantSerializer(serializers.ModelSerializer):
    """
    Serializer for Tenant model with multi-tenant configuration management.
    
    Provides serialization for tenant information including configuration
    settings and status information.
    """
    
    # Read-only fields for security and data integrity
    id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    
    # Computed fields
    user_count = serializers.SerializerMethodField()
    session_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Tenant
        fields = [
            'id',
            'name',
            'slug',
            'subdomain',
            'is_active',
            'asterisk_host',
            'ami_port',
            'ami_username',
            'ami_password',
            'ari_port',
            'ari_username', 
            'ari_password',
            'ari_app_name',
            'configuration',
            'created_at',
            'updated_at',
            'user_count',
            'session_count',
        ]
        
        # Sensitive fields that should be write-only
        extra_kwargs = {
            'ami_password': {'write_only': True},
            'ari_password': {'write_only': True},
            'slug': {'required': False},  # Auto-generated if not provided
        }
    
    def get_user_count(self, obj):
        """
        Get the number of active users in this tenant.
        
        Args:
            obj: Tenant instance
            
        Returns:
            int: Number of active users
        """
        return obj.userprofile_set.filter(user__is_active=True).count()
    
    def get_session_count(self, obj):
        """
        Get the number of call sessions for this tenant.
        
        Args:
            obj: Tenant instance
            
        Returns:
            int: Number of call sessions
        """
        return obj.callsession_set.count()
    
    def validate_subdomain(self, value):
        """
        Validate that the subdomain is unique and follows naming conventions.
        
        Args:
            value: The subdomain value to validate
            
        Returns:
            str: The validated subdomain value
            
        Raises:
            ValidationError: If subdomain is invalid
        """
        if value:
            # Check if subdomain already exists (excluding current instance)
            existing = Tenant.objects.filter(subdomain=value)
            if self.instance:
                existing = existing.exclude(id=self.instance.id)
            
            if existing.exists():
                raise serializers.ValidationError("This subdomain is already in use.")
            
            # Validate subdomain format (alphanumeric and hyphens only)
            import re
            if not re.match(r'^[a-z0-9-]+$', value):
                raise serializers.ValidationError(
                    "Subdomain can only contain lowercase letters, numbers, and hyphens."
                )
            
            # Reserved subdomains
            reserved = ['www', 'api', 'admin', 'mail', 'ftp', 'test', 'staging']
            if value in reserved:
                raise serializers.ValidationError(f"'{value}' is a reserved subdomain.")
        
        return value
    
    def validate_slug(self, value):
        """
        Validate that the slug is unique and follows naming conventions.
        
        Args:
            value: The slug value to validate
            
        Returns:
            str: The validated slug value
            
        Raises:
            ValidationError: If slug is invalid
        """
        if value:
            # Check if slug already exists (excluding current instance)
            existing = Tenant.objects.filter(slug=value)
            if self.instance:
                existing = existing.exclude(id=self.instance.id)
            
            if existing.exists():
                raise serializers.ValidationError("This slug is already in use.")
        
        return value
    
    def validate_configuration(self, value):
        """
        Validate tenant configuration JSON structure.
        
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
            
            # Validate required configuration keys
            required_keys = ['recording_enabled', 'transcription_enabled']
            for key in required_keys:
                if key not in value:
                    raise serializers.ValidationError(f"Configuration missing required key: {key}")
            
            # Validate configuration values
            if not isinstance(value.get('recording_enabled'), bool):
                raise serializers.ValidationError("recording_enabled must be a boolean value.")
            
            if not isinstance(value.get('transcription_enabled'), bool):
                raise serializers.ValidationError("transcription_enabled must be a boolean value.")
        
        return value
    
    def create(self, validated_data):
        """
        Create a new tenant with auto-generated slug if not provided.
        
        Args:
            validated_data: Validated data from the serializer
            
        Returns:
            Tenant: The created tenant instance
        """
        # Auto-generate slug from name if not provided
        if not validated_data.get('slug'):
            from django.utils.text import slugify
            base_slug = slugify(validated_data['name'])
            slug = base_slug
            counter = 1
            
            # Ensure slug is unique
            while Tenant.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            
            validated_data['slug'] = slug
        
        return super().create(validated_data)
