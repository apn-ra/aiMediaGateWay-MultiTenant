"""
Tenant serializers for aiMediaGateway

This module contains serializers for the Tenant model with multi-tenant
configuration management capabilities.
"""
import time
import logging

from rest_framework import serializers
from core.models import Tenant
from core.ami.manager import AMIConnection, AMIConnectionConfig

logger = logging.getLogger(__name__)

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

    class Meta:
        model = Tenant
        fields = [
            'id',
            'name',
            'slug',
            'is_active',
            'host',
            'ami_port',
            'ami_username',
            'ami_secret',
            'ari_port',
            'ari_username', 
            'ari_password',
            'ari_app_name',
            'created_at',
            'updated_at',
        ]
        
        # Sensitive fields that should be write-only
        extra_kwargs = {
            'ami_password': {'write_only': True},
            'ari_password': {'write_only': True},
            'slug': {'required': False},  # Auto-generated if not provided
        }

    def get_session_count(self, obj):
        """
        Get the number of call sessions for this tenant.
        
        Args:
            obj: Tenant instance
            
        Returns:
            int: Number of call sessions
        """
        return obj.callsession_set.count()

    @staticmethod
    def _validate_ami_credentials(host, port, username, secret):
        try:
            if host and username and secret:
                config = AMIConnectionConfig(
                    host=host,
                    port=port or 5038,
                    username=username,
                    secret=secret,
                    timeout=10.0,
                    max_retries=1,
                    retry_delay=1.0,
                    ping_interval=5.0
                )

                ami_test = AMIConnection(tenant_id=0, config=config)
                ami_test.test_connect()
                time.sleep(0.05)
                ami_test.manager.close()
                return True
        except Exception as e:
            logger.error(f"Error validating AMI credentials: {e}")
            raise serializers.ValidationError("Error Validating AMI Credentials") from e

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

    def validate(self, attrs):
        host = attrs.get('host')

        ami_username = attrs.get('ami_username')
        ami_secret = attrs.get('ami_secret')
        ami_port = attrs.get('ami_port')

        ari_username = attrs.get('ari_username')
        ari_password = attrs.get('ari_password')
        ari_port = attrs.get('ari_port')


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
