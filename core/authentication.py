"""
Custom authentication classes for aiMediaGateway API

This module provides additional authentication methods beyond the standard
DRF authentication classes to support various client types and integration
scenarios in the multi-tenant PBX system.
"""

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth.models import User
from django.conf import settings
import hashlib
import hmac
from core.models import Tenant


class APIKeyAuthentication(BaseAuthentication):
    """
    API Key Authentication for third-party integrations
    
    This authentication method allows external systems to authenticate
    using API keys associated with specific tenants. Each API key is
    tied to a tenant and provides programmatic access to that tenant's
    resources.
    
    Usage:
        Include the API key in the request header:
        Authorization: ApiKey <api_key>
        
    Or as a query parameter:
        ?api_key=<api_key>
    """
    
    keyword = 'ApiKey'
    
    def authenticate(self, request):
        """
        Authenticate the request using API key
        
        Returns:
            tuple: (user, auth) if authentication successful
            None: if authentication not attempted or failed
        """
        # Try to get API key from Authorization header
        api_key = self.get_api_key_from_header(request)
        
        # If not found in header, try query parameter
        if not api_key:
            api_key = request.GET.get('api_key')
        
        if not api_key:
            return None
            
        return self.authenticate_credentials(api_key)
    
    def get_api_key_from_header(self, request):
        """Extract API key from Authorization header"""
        auth_header = request.META.get('HTTP_AUTHORIZATION')
        if not auth_header:
            return None
            
        try:
            keyword, api_key = auth_header.split()
            if keyword.lower() != self.keyword.lower():
                return None
            return api_key
        except ValueError:
            return None
    
    def authenticate_credentials(self, api_key):
        """
        Validate API key and return user/auth tuple
        
        Args:
            api_key (str): The API key to validate
            
        Returns:
            tuple: (user, tenant) if valid
            
        Raises:
            AuthenticationFailed: If API key is invalid or expired
        """
        try:
            # Hash the API key for secure comparison
            api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
            
            # Find tenant with matching API key hash
            # Note: In production, store API keys securely hashed in database
            tenant = Tenant.objects.filter(
                api_key_hash=api_key_hash,
                is_active=True
            ).first()
            
            if not tenant:
                raise AuthenticationFailed('Invalid API key')
            
            # For API key authentication, we use a system user
            # or create a special API user for the tenant
            api_user = self.get_or_create_api_user(tenant)
            
            return (api_user, tenant)
            
        except Exception as e:
            raise AuthenticationFailed(f'API key authentication failed: {str(e)}')
    
    def get_or_create_api_user(self, tenant):
        """
        Get or create a system user for API access
        
        Args:
            tenant: The tenant for which to create API user
            
        Returns:
            User: Django user instance for API access
        """
        username = f"api_user_{tenant.slug}"
        
        try:
            api_user = User.objects.get(username=username)
        except User.DoesNotExist:
            # Create API user for this tenant
            api_user = User.objects.create_user(
                username=username,
                email=f"api@{tenant.domain}",
                password=None,  # No password for API users
                is_staff=False,
                is_active=True
            )
            
            # Create UserProfile for the API user
            from core.models import UserProfile
            UserProfile.objects.create(
                user=api_user,
                tenant=tenant,
                role='api_user',  # Special role for API access
                phone_number='',
                is_active=True
            )
        
        return api_user
    
    def authenticate_header(self, request):
        """
        Return authentication header for 401 responses
        """
        return self.keyword


class TenantAPIKeyAuthentication(APIKeyAuthentication):
    """
    Enhanced API Key Authentication with tenant isolation
    
    This authentication method extends APIKeyAuthentication to provide
    additional tenant-specific validation and context setting for
    multi-tenant API access.
    """
    
    def authenticate(self, request):
        """
        Authenticate and set tenant context
        """
        result = super().authenticate(request)
        
        if result:
            user, tenant = result
            # Set tenant context in request for middleware
            request.tenant = tenant
            request.tenant_user = user
            
        return result
    
    def authenticate_credentials(self, api_key):
        """
        Enhanced credential validation with tenant context
        """
        user, tenant = super().authenticate_credentials(api_key)
        
        # Additional tenant-specific validation
        if not tenant.api_access_enabled:
            raise AuthenticationFailed('API access disabled for this tenant')
        
        # Check API rate limiting (if implemented)
        if hasattr(tenant, 'api_rate_limit') and tenant.api_rate_limit:
            # Rate limiting logic would go here
            pass
        
        return (user, tenant)


class SignedRequestAuthentication(BaseAuthentication):
    """
    Signed Request Authentication for high-security integrations
    
    This authentication method uses request signing with shared secrets
    to provide cryptographic verification of request authenticity.
    Useful for webhook callbacks and high-security API integrations.
    """
    
    def authenticate(self, request):
        """
        Authenticate using request signature
        """
        signature = request.META.get('HTTP_X_SIGNATURE')
        timestamp = request.META.get('HTTP_X_TIMESTAMP')
        tenant_id = request.META.get('HTTP_X_TENANT_ID')
        
        if not all([signature, timestamp, tenant_id]):
            return None
        
        return self.authenticate_signature(request, signature, timestamp, tenant_id)
    
    def authenticate_signature(self, request, signature, timestamp, tenant_id):
        """
        Validate request signature
        
        Args:
            request: Django request object
            signature: Request signature from header
            timestamp: Request timestamp
            tenant_id: Tenant identifier
            
        Returns:
            tuple: (user, tenant) if valid signature
            
        Raises:
            AuthenticationFailed: If signature validation fails
        """
        try:
            # Get tenant and shared secret
            tenant = Tenant.objects.get(id=tenant_id, is_active=True)
            shared_secret = tenant.webhook_secret
            
            if not shared_secret:
                raise AuthenticationFailed('Webhook secret not configured')
            
            # Reconstruct expected signature
            payload = request.body
            message = f"{timestamp}.{payload.decode('utf-8')}"
            
            expected_signature = hmac.new(
                shared_secret.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            # Verify signature
            if not hmac.compare_digest(signature, expected_signature):
                raise AuthenticationFailed('Invalid request signature')
            
            # Check timestamp to prevent replay attacks (5 minute window)
            import time
            current_time = int(time.time())
            request_time = int(timestamp)
            
            if abs(current_time - request_time) > 300:  # 5 minutes
                raise AuthenticationFailed('Request timestamp too old')
            
            # Get system user for webhook
            webhook_user = self.get_webhook_user(tenant)
            
            return (webhook_user, tenant)
            
        except Tenant.DoesNotExist:
            raise AuthenticationFailed('Invalid tenant ID')
        except Exception as e:
            raise AuthenticationFailed(f'Signature authentication failed: {str(e)}')
    
    def get_webhook_user(self, tenant):
        """
        Get or create webhook user for signed requests
        """
        username = f"webhook_user_{tenant.slug}"
        
        try:
            webhook_user = User.objects.get(username=username)
        except User.DoesNotExist:
            webhook_user = User.objects.create_user(
                username=username,
                email=f"webhook@{tenant.domain}",
                password=None,
                is_staff=False,
                is_active=True
            )
            
            from core.models import UserProfile
            UserProfile.objects.create(
                user=webhook_user,
                tenant=tenant,
                role='webhook_user',
                phone_number='',
                is_active=True
            )
        
        return webhook_user
    
    def authenticate_header(self, request):
        """
        Return authentication header for 401 responses
        """
        return 'Signature'
