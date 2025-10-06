"""
WebSocket Authentication Middleware for aiMediaGateway

This module provides comprehensive authentication and security middleware for WebSocket connections,
including JWT token validation, tenant context resolution, rate limiting, and origin validation.
"""

import json
import logging
import time
from urllib.parse import parse_qs
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.conf import settings
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from rest_framework_simplejwt.tokens import UntypedToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from jwt import decode as jwt_decode

from core.models import Tenant, UserProfile

User = get_user_model()
logger = logging.getLogger(__name__)


class WebSocketJWTAuthMiddleware(BaseMiddleware):
    """
    JWT Authentication middleware for WebSocket connections.
    Validates JWT tokens from query parameters or headers.
    """
    
    async def __call__(self, scope, receive, send):
        # Only process WebSocket connections
        if scope["type"] != "websocket":
            return await super().__call__(scope, receive, send)
        
        # Extract token from query parameters or headers
        token = self._extract_token(scope)
        
        if token:
            try:
                # Validate and decode JWT token
                user = await self._get_user_from_token(token)
                scope["user"] = user
                scope["token"] = token
            except (InvalidToken, TokenError, Exception) as e:
                logger.warning(f"WebSocket JWT authentication failed: {e}")
                scope["user"] = AnonymousUser()
        else:
            scope["user"] = AnonymousUser()
        
        return await super().__call__(scope, receive, send)
    
    def _extract_token(self, scope):
        """Extract JWT token from query parameters or headers."""
        # Try query parameters first
        query_string = scope.get("query_string", b"").decode()
        query_params = parse_qs(query_string)
        
        # Check for token in query parameters
        if "token" in query_params:
            return query_params["token"][0]
        
        # Check for Authorization header
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()
        
        if auth_header.startswith("Bearer "):
            return auth_header[7:]  # Remove "Bearer " prefix
        
        return None
    
    @database_sync_to_async
    def _get_user_from_token(self, token):
        """Validate JWT token and return associated user."""
        try:
            # Validate token
            UntypedToken(token)
            
            # Decode token to get user information
            decoded_token = jwt_decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"]
            )
            
            user_id = decoded_token.get("user_id")
            if user_id:
                user = User.objects.get(id=user_id)
                return user
            
        except User.DoesNotExist:
            logger.warning(f"User not found for token user_id: {user_id}")
        except Exception as e:
            logger.error(f"Error validating WebSocket JWT token: {e}")
        
        return AnonymousUser()


class WebSocketTenantMiddleware(BaseMiddleware):
    """
    Tenant context resolution middleware for WebSocket connections.
    Resolves tenant from subdomain, header, or user profile.
    """
    
    async def __call__(self, scope, receive, send):
        # Only process WebSocket connections
        if scope["type"] != "websocket":
            return await super().__call__(scope, receive, send)
        
        # Get tenant context
        tenant = await self._resolve_tenant(scope)
        scope["tenant"] = tenant
        scope["tenant_id"] = tenant.id if tenant else None
        
        return await super().__call__(scope, receive, send)
    
    async def _resolve_tenant(self, scope):
        """Resolve tenant from various sources."""
        # Try to get tenant from subdomain
        host = self._get_host(scope)
        if host:
            tenant = await self._get_tenant_by_subdomain(host)
            if tenant:
                return tenant
        
        # Try to get tenant from query parameters
        query_string = scope.get("query_string", b"").decode()
        query_params = parse_qs(query_string)
        
        if "tenant" in query_params:
            tenant_identifier = query_params["tenant"][0]
            tenant = await self._get_tenant_by_identifier(tenant_identifier)
            if tenant:
                return tenant
        
        # Try to get tenant from user profile
        user = scope.get("user")
        if user and not isinstance(user, AnonymousUser):
            tenant = await self._get_tenant_from_user(user)
            if tenant:
                return tenant
        
        return None
    
    def _get_host(self, scope):
        """Extract host from WebSocket scope."""
        headers = dict(scope.get("headers", []))
        host_header = headers.get(b"host", b"").decode()
        return host_header
    
    @database_sync_to_async
    def _get_tenant_by_subdomain(self, host):
        """Get tenant by subdomain from host header."""
        try:
            subdomain = host.split('.')[0]
            return Tenant.objects.get(subdomain=subdomain, is_active=True)
        except (Tenant.DoesNotExist, IndexError):
            return None
    
    @database_sync_to_async
    def _get_tenant_by_identifier(self, identifier):
        """Get tenant by slug or ID."""
        try:
            # Try by slug first
            return Tenant.objects.get(slug=identifier, is_active=True)
        except Tenant.DoesNotExist:
            try:
                # Try by ID
                return Tenant.objects.get(id=int(identifier), is_active=True)
            except (Tenant.DoesNotExist, ValueError):
                return None
    
    @database_sync_to_async
    def _get_tenant_from_user(self, user):
        """Get tenant from user profile."""
        try:
            user_profile = UserProfile.objects.get(user=user)
            return user_profile.tenant if user_profile.tenant.is_active else None
        except UserProfile.DoesNotExist:
            return None


class WebSocketRateLimitMiddleware(BaseMiddleware):
    """
    Rate limiting middleware for WebSocket connections.
    Prevents abuse by limiting connection attempts per IP/user.
    """
    
    def __init__(self, inner):
        super().__init__(inner)
        self.rate_limit_window = getattr(settings, 'WEBSOCKET_RATE_LIMIT_WINDOW', 60)  # 60 seconds
        self.rate_limit_max_connections = getattr(settings, 'WEBSOCKET_RATE_LIMIT_MAX', 10)  # 10 connections per window
    
    async def __call__(self, scope, receive, send):
        # Only process WebSocket connections
        if scope["type"] != "websocket":
            return await super().__call__(scope, receive, send)
        
        # Get client identifier
        client_id = self._get_client_identifier(scope)
        
        # Check rate limit
        if not await self._check_rate_limit(client_id):
            # Rate limit exceeded, close connection
            logger.warning(f"WebSocket rate limit exceeded for client: {client_id}")
            await send({
                "type": "websocket.close",
                "code": 4029  # Custom code for rate limiting
            })
            return
        
        # Record connection attempt
        await self._record_connection_attempt(client_id)
        
        return await super().__call__(scope, receive, send)
    
    def _get_client_identifier(self, scope):
        """Get unique client identifier for rate limiting."""
        # Get user if authenticated
        user = scope.get("user")
        if user and not isinstance(user, AnonymousUser):
            return f"user_{user.id}"
        
        # Fall back to IP address
        client_ip = self._get_client_ip(scope)
        return f"ip_{client_ip}"
    
    def _get_client_ip(self, scope):
        """Extract client IP from scope."""
        # Check for forwarded headers first
        headers = dict(scope.get("headers", []))
        
        forwarded_for = headers.get(b"x-forwarded-for", b"").decode()
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = headers.get(b"x-real-ip", b"").decode()
        if real_ip:
            return real_ip
        
        # Fall back to direct client
        client = scope.get("client", ["unknown", 0])
        return client[0]
    
    async def _check_rate_limit(self, client_id):
        """Check if client has exceeded rate limit."""
        cache_key = f"websocket_rate_limit_{client_id}"
        current_time = int(time.time())
        window_start = current_time - self.rate_limit_window
        
        # Get connection attempts from cache
        attempts = cache.get(cache_key, [])
        
        # Filter attempts within the current window
        recent_attempts = [attempt for attempt in attempts if attempt > window_start]
        
        # Check if limit exceeded
        return len(recent_attempts) < self.rate_limit_max_connections
    
    async def _record_connection_attempt(self, client_id):
        """Record a connection attempt for rate limiting."""
        cache_key = f"websocket_rate_limit_{client_id}"
        current_time = int(time.time())
        window_start = current_time - self.rate_limit_window
        
        # Get existing attempts
        attempts = cache.get(cache_key, [])
        
        # Filter recent attempts and add current
        recent_attempts = [attempt for attempt in attempts if attempt > window_start]
        recent_attempts.append(current_time)
        
        # Update cache
        cache.set(cache_key, recent_attempts, timeout=self.rate_limit_window * 2)


class WebSocketOriginValidationMiddleware(BaseMiddleware):
    """
    Origin validation middleware for WebSocket connections.
    Validates request origin against allowed origins for security.
    """
    
    def __init__(self, inner):
        super().__init__(inner)
        self.allowed_origins = getattr(settings, 'WEBSOCKET_ALLOWED_ORIGINS', [])
        self.allow_any_origin = getattr(settings, 'WEBSOCKET_ALLOW_ANY_ORIGIN', False)
    
    async def __call__(self, scope, receive, send):
        # Only process WebSocket connections
        if scope["type"] != "websocket":
            return await super().__call__(scope, receive, send)
        
        # Skip validation if any origin is allowed (development mode)
        if self.allow_any_origin:
            return await super().__call__(scope, receive, send)
        
        # Validate origin
        if not self._validate_origin(scope):
            logger.warning(f"WebSocket connection rejected due to invalid origin")
            await send({
                "type": "websocket.close",
                "code": 4003  # Custom code for origin validation failure
            })
            return
        
        return await super().__call__(scope, receive, send)
    
    def _validate_origin(self, scope):
        """Validate request origin against allowed origins."""
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode()
        
        if not origin:
            # No origin header - could be a direct connection
            return True
        
        # Check against allowed origins
        if self.allowed_origins:
            return any(self._origin_matches(origin, allowed) for allowed in self.allowed_origins)
        
        # If no specific origins configured, allow same-origin
        host = headers.get(b"host", b"").decode()
        if host:
            expected_origin = f"https://{host}"  # Assume HTTPS in production
            return origin == expected_origin or origin == f"http://{host}"  # Allow HTTP for development
        
        return False
    
    def _origin_matches(self, origin, allowed_pattern):
        """Check if origin matches allowed pattern (supports wildcards)."""
        if allowed_pattern == "*":
            return True
        
        if allowed_pattern.startswith("*."):
            # Wildcard subdomain matching
            domain = allowed_pattern[2:]
            return origin.endswith(f".{domain}") or origin == f"https://{domain}" or origin == f"http://{domain}"
        
        return origin == allowed_pattern


def WebSocketAuthMiddlewareStack(inner):
    """
    Composite middleware stack for WebSocket authentication and security.
    Applies all authentication and security middleware in the correct order.
    """
    return WebSocketOriginValidationMiddleware(
        WebSocketRateLimitMiddleware(
            WebSocketTenantMiddleware(
                WebSocketJWTAuthMiddleware(inner)
            )
        )
    )
