"""
Tenant serializers for aiMediaGateway

This module contains serializers for the Tenant model with multi-tenant
configuration management capabilities.
"""

import logging
import re
import socket
import requests
from typing import Optional
from rest_framework import serializers
from requests.auth import HTTPBasicAuth
from core.models import Tenant

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
    def _validate_ari_credentials(host:str, username:str, password:str, port:int = 8088, use_https:bool = False, min_version:int = 18, timeout:int = 3) -> Optional[bool]:
        """Validate Asterisk ARI credentials by performing an authenticated GET request."""
        protocol = "https" if use_https else "http"
        url = f"{protocol}://{host}:{port}/ari/asterisk/info"

        try:
            response = requests.get(url, auth=HTTPBasicAuth(username, password), timeout=timeout)

            if response.status_code == 200:
                info = response.json()
                version_str = info.get("build", {}).get("version", "")
                logger.debug(f"ARI Received version: {version_str}")

                match = re.match(r"(\d+)\.(\d+)", version_str)
                if match:
                    major = int(match.group(1))
                    minor = int(match.group(2))
                    if major >= min_version:
                        return True
                    else:
                        raise serializers.ValidationError(f"Asterisk version {major}.{minor} is too old. Minimum required version: {min_version}")
                else:
                    raise serializers.ValidationError("Could not detect Asterisk version from response.")

            elif response.status_code == 401:
                raise serializers.ValidationError("Authentication Failed")
            else:
                raise serializers.ValidationError(f"Unexpected response {response.status_code}: {response.text}")

        except requests.exceptions.ConnectionError:
            raise serializers.ValidationError("Connection failed — ARI may not be running or port is wrong.")
        except Exception as e:
            raise serializers.ValidationError(f"Error: Validating ARI Credentials") from e

    @staticmethod
    def _validate_ami_credentials(host:str, username:str, secret:str, port:int=5038, min_version:int = 18, timeout:int = 3) -> Optional[bool]:
        """Validate Asterisk AMI credentials by attempting to log in."""
        try:
            # Connect to AMI socket
            s = socket.create_connection((host, port), timeout=timeout)

            # Receive the banner line (e.g., "Asterisk Call Manager/18.15.0")
            banner = s.recv(1024).decode(errors="ignore")
            logger.debug(f"AMI Received banner: {banner.strip()}")

            # Extract version
            match = re.search(r"Asterisk Call Manager/(\d+)\.(\d+)", banner)
            if not match:
                s.close()
                raise serializers.ValidationError("Could not detect Asterisk version from banner.")

            major = int(match.group(1))
            minor = int(match.group(2))
            if major < min_version:
                s.close()
                raise serializers.ValidationError(f"Asterisk version {major}.{minor} is too old. Minimum required version: {min_version}")

            # Login action
            login_action = f"Action: Login\r\nUsername: {username}\r\nSecret: {secret}\r\nEvents: off\r\n\r\n"
            s.sendall(login_action.encode())

            # Read response
            response = s.recv(1024).decode(errors="ignore")
            s.close()

            # Check response
            if "Message: Authentication accepted" in response:
                return True
            elif "Message: Authentication failed" in response:
                raise serializers.ValidationError("Authentication Failed")
            else:
                raise serializers.ValidationError("Validating AMI Credentials")

        except (socket.timeout, ConnectionRefusedError) as e:
            raise serializers.ValidationError(f"Connecting to {host} Asterisk AMI") from e
        except Exception as e:
            raise serializers.ValidationError(f"Validating AMI Credentials") from e

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

        ami_validation = self._validate_ami_credentials(host, ami_port, ami_username, ami_secret)
        ari_validation = self._validate_ari_credentials(host, ari_port, ari_username, ari_password)

        if not ami_validation or not ari_validation:
            raise serializers.ValidationError("Invalid Asterisk credentials.")

        return attrs
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
