"""
Middleware for multi-tenant API isolation in aiMediaGateway

This module provides middleware for identifying and isolating tenants
in API requests using multiple identification strategies.
"""

import jwt
import logging
from django.conf import settings
from django.http import JsonResponse
from django.contrib.auth import get_user_model
from django.utils.deprecation import MiddlewareMixin
from django.core.cache import cache
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError

from core.models import Tenant

logger = logging.getLogger(__name__)

User = get_user_model()

class TenantAPIMiddleware(MiddlewareMixin):
    """
    Middleware for tenant isolation in API requests.
    
    Identifies tenants using multiple strategies:
    1. Subdomain-based identification (e.g., tenant1.api.domain.com)
    2. HTTP header-based identification (X-Tenant-ID)
    3. URL parameter-based identification (?tenant=tenant_id)
    
    Sets the current tenant in the request context for use by views and permissions.
    """
    
    def process_request(self, request):
        """
        Process incoming request to identify and set tenant context.
        
        Args:
            request: Django HttpRequest object
            
        Returns:
            None if processing should continue, HttpResponse if error
        """
        # Skip tenant identification for non-API requests
        if not request.path.startswith('/api/'):
            return None
            
        # Skip tenant identification for admin and auth endpoints
        if request.path.startswith('/admin/') or 'auth' in request.path:
            return None
            
        tenant = None
        tenant_identifier = None
        identification_method = None
        
        try:
            if request.user.is_superuser or request.user.is_staff:
                return None

            if not tenant:
                tenant, tenant_identifier, identification_method = self._identify_by_header(request)
            
            # Method 3: URL parameter-based identification (if header failed)
            if not tenant:
                tenant, tenant_identifier, identification_method = self._identify_by_url_parameter(request)
            
            # If no tenant identified, return error for API endpoints
            if not tenant:
                logger.warning(f"No tenant identified for API request: {request.path}")
                return JsonResponse({
                    'error': 'Tenant identification required',
                    'detail': 'API requests must include tenant identification via X-Tenant-ID header, or tenant URL parameter'
                }, status=400)
            
            # Set tenant context in request
            request.tenant = tenant
            request.tenant_identifier = tenant_identifier
            request.tenant_identification_method = identification_method
            
            logger.debug(f"Tenant identified: {tenant.name} (ID: {tenant.id}) via {identification_method}")
            
        except Exception as e:
            logger.error(f"Error in tenant identification: {str(e)}")
            return JsonResponse({
                'error': 'Tenant identification error',
                'detail': 'Unable to process tenant identification'
            }, status=500)
            
        return None
    
    def _identify_by_subdomain(self, request):
        """
        Identify tenant by subdomain (e.g., tenant1.api.domain.com)
        
        Args:
            request: Django HttpRequest object
            
        Returns:
            tuple: (tenant_object, identifier, method) or (None, None, None)
        """
        host = request.get_host().lower()
        
        # Extract subdomain (assuming format: subdomain.domain.com)
        parts = host.split('.')
        if len(parts) >= 3:
            subdomain = parts[0]
            
            # Skip common subdomains
            if subdomain in ['www', 'api', 'admin']:
                return None, None, None
                
            # Cache key for tenant lookup
            cache_key = f"tenant_subdomain_{subdomain}"
            tenant = cache.get(cache_key)
            
            if tenant is None:
                try:
                    tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
                    cache.set(cache_key, tenant, 300)  # Cache for 5 minutes
                except Tenant.DoesNotExist:
                    return None, None, None
            
            return tenant, subdomain, 'subdomain'
        
        return None, None, None
    
    def _identify_by_header(self, request):
        """
        Identify tenant by X-Tenant-ID header
        
        Args:
            request: Django HttpRequest object
            
        Returns:
            tuple: (tenant_object, identifier, method) or (None, None, None)
        """
        tenant_id = request.META.get('HTTP_X_TENANT_ID')
        
        if tenant_id:
            # Cache key for tenant lookup
            cache_key = f"tenant_header_{tenant_id}"
            tenant = cache.get(cache_key)
            
            if tenant is None:
                try:
                    # Try to get tenant by ID first, then by slug
                    if tenant_id.isdigit():
                        tenant = Tenant.objects.get(id=int(tenant_id), is_active=True)
                    else:
                        tenant = Tenant.objects.get(slug=tenant_id, is_active=True)
                    
                    cache.set(cache_key, tenant, 300)  # Cache for 5 minutes
                except Tenant.DoesNotExist:
                    return None, None, None
            
            return tenant, tenant_id, 'header'
        
        return None, None, None
    
    def _identify_by_url_parameter(self, request):
        """
        Identify tenant by URL parameter (?tenant=tenant_id)
        
        Args:
            request: Django HttpRequest object
            
        Returns:
            tuple: (tenant_object, identifier, method) or (None, None, None)
        """
        tenant_id = request.GET.get('tenant')
        
        if tenant_id:
            # Cache key for tenant lookup
            cache_key = f"tenant_param_{tenant_id}"
            tenant = cache.get(cache_key)
            
            if tenant is None:
                try:
                    # Try to get tenant by ID first, then by slug
                    if tenant_id.isdigit():
                        tenant = Tenant.objects.get(id=int(tenant_id), is_active=True)
                    else:
                        tenant = Tenant.objects.get(name=tenant_id, is_active=True)
                    
                    cache.set(cache_key, tenant, 300)  # Cache for 5 minutes
                except Tenant.DoesNotExist:
                    return None, None, None
            
            return tenant, tenant_id, 'url_parameter'
        
        return None, None, None
    
    def process_response(self, request, response):
        """
        Process response to add tenant context headers if available.
        
        Args:
            request: Django HttpRequest object
            response: Django HttpResponse object
            
        Returns:
            HttpResponse object with added headers
        """
        if hasattr(request, 'tenant') and request.tenant:
            response['X-Tenant-Context'] = request.tenant.name
            response['X-Tenant-Method'] = getattr(request, 'tenant_identification_method', 'unknown')
        
        return response


class CombinedAuthMiddleware:
    """
    Non-blocking middleware that supports Django session auth + SimpleJWT access tokens.
    Important: does NOT return 401 on invalid/missing tokens — let views handle that.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.jwt_auth = JWTAuthentication()

    def __call__(self, request):
        # If AuthenticationMiddleware already set request.user (session or other), keep it
        if hasattr(request, "user") and request.user.is_authenticated:
            return self.get_response(request)

        # Try to load an access token from Authorization header if present
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(' ', 1)[1].strip()
            try:
                validated_token = self.jwt_auth.get_validated_token(token)
                user = self.jwt_auth.get_user(validated_token)
                # Attach user and validated token so downstream code can use request.auth
                request.user = user
                request.auth = validated_token
                request.auth_type = 'jwt'
            except TokenError:
                # Token invalid/expired: do NOT block — let refresh endpoint handle refresh
                request.auth_type = 'jwt_invalid'
                # Optionally set a flag so views know token was present but invalid:
                # request._jwt_error = str(e)

        return self.get_response(request)
