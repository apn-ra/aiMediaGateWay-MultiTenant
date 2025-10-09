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
            'domain',
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
            'domain': {'required': True},
            'ami_secret': {'write_only': True},
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
                version_str = info.get("system", {}).get("version", "")
                logger.debug(f"ARI Received version: {version_str}")

                match = re.match(r"(\d+)\.(\d+)", version_str)
                if match:
                    major = int(match.group(1))
                    minor = int(match.group(2))
                    if major >= min_version:
                        return True
                    else:
                        raise Exception(f"Asterisk version {major}.{minor} is too old. Asterisk Minimum required version: {min_version}")
                else:
                    raise Exception("Could not detect Asterisk version from response.")

            elif response.status_code == 401:
                raise Exception("Authentication Failed")
            else:
                raise Exception(f"Unexpected response {response.status_code}: {response.text}")

        except requests.exceptions.ConnectionError:
            raise Exception("Connection failed — ARI may not be running or port is wrong.")
        except Exception as e:
            raise serializers.ValidationError({
                "host": f"{host}",
                "error": str(e),
            }) from e

    @staticmethod
    def _validate_ami_credentials(host:str, username:str, secret:str, port:int=5038, min_version:int = 11, timeout:int = 3) -> Optional[bool]:
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
                raise Exception(f"Asterisk version {major}.{minor} is too old. Asterisk Call Manager Minimum required version: {min_version}")

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
                raise Exception("Authentication Failed")
            else:
                raise Exception("Validating AMI Credentials")

        except (socket.timeout, ConnectionRefusedError) as e:
            raise Exception(str(e))
        except Exception as e:
            raise serializers.ValidationError({"host": f"{host}", "error": str(e)}) from e

    def validate_domain(self, value):
        # Basic domain regex (no scheme like http://)
        domain_pattern = re.compile(
            r'^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$'
        )
        if not domain_pattern.match(value):
            raise serializers.ValidationError("Invalid domain format.")

        # Optional: ensure it's not an IP or malformed URL
        if ' ' in value or '/' in value:
            raise serializers.ValidationError("Domain must not contain spaces or slashes.")

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

    def validate(self, attrs):
        host = attrs.get('host')

        ami_username = attrs.get('ami_username')
        ami_secret = attrs.get('ami_secret')
        ami_port = attrs.get('ami_port') or 5038

        ari_username = attrs.get('ari_username')
        ari_password = attrs.get('ari_password')
        ari_port = attrs.get('ari_port') or 8088

        ami_validation = self._validate_ami_credentials(
            host=host,
            username=ami_username,
            secret=ami_secret,
            port=ami_port,
        )
        ari_validation = self._validate_ari_credentials(
            host=host,
            username=ari_username,
            password= ari_password,
            port=ari_port,
        )

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
            validated_data['ari_app_name'] = f"{slug}-app"
        return super().create(validated_data)
