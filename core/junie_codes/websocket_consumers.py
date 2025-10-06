"""
WebSocket consumers for real-time features in the AI Media Gateway system.

This module provides comprehensive WebSocket consumer implementations
for real-time call monitoring, live audio streaming, system status updates,
and admin notifications with multi-tenant support.

Features:
- Real-time call monitoring and status updates
- Live audio streaming via WebSocket
- System status and health monitoring
- Admin notifications and alerts
- Multi-tenant channel group isolation
- Authentication and authorization
- Connection management and cleanup
"""

import logging
import json
from typing import Dict, Optional, Any
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist

# Import our components
from core.session.manager import get_session_manager
from core.junie_codes.audio.audio_streaming import get_streaming_manager, StreamingProtocol, StreamQuality, StreamingFormat
from core.junie_codes.audio.audio_recording import get_recording_manager
from core.junie_codes.audio.audio_quality import get_quality_manager
from core.models import UserProfile, Tenant

logger = logging.getLogger(__name__)


class BaseTenantConsumer(AsyncWebsocketConsumer):
    """
    Base WebSocket consumer with multi-tenant support, authentication,
    error handling, and connection lifecycle management.
    
    This class provides common functionality for all WebSocket consumers
    in the aiMediaGateway system including:
    - Tenant context resolution
    - User authentication and authorization
    - Error handling and logging
    - Connection lifecycle management
    - Rate limiting and throttling
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.tenant = None
        self.tenant_id = None
        self.user_profile = None
        self.channel_groups = set()
        self.connection_id = None
        self.connected_at = None
        self.message_count = 0
        self.last_activity = None
        
    async def connect(self):
        """
        Base connection handler with tenant context and authentication.
        Subclasses should call super().connect() first, then add their specific logic.
        """
        try:
            # Generate unique connection ID
            self.connection_id = f"{self.channel_name}_{timezone.now().timestamp()}"
            self.connected_at = timezone.now()
            self.last_activity = timezone.now()
            
            # Extract user and tenant from scope
            self.user = self.scope.get("user", AnonymousUser())
            self.tenant_id = self.scope.get("tenant_id")
            
            # Resolve tenant context
            if not await self._resolve_tenant_context():
                await self._close_with_error(4001, "Tenant context resolution failed")
                return False
            
            # Authenticate user
            if not await self._authenticate_user():
                await self._close_with_error(4001, "Authentication failed")
                return False
            
            # Check user permissions
            if not await self._check_permissions():
                await self._close_with_error(4003, "Insufficient permissions")
                return False
            
            # Accept connection
            await self.accept()
            
            # Send connection established message
            await self._send_connection_established()
            
            logger.info(f"WebSocket connected: {self.__class__.__name__} for user {self.user.username} in tenant {self.tenant.slug}")
            return True
            
        except Exception as e:
            logger.error(f"Error in {self.__class__.__name__} connect: {e}")
            await self._close_with_error(4000, "Internal server error")
            return False
    
    async def disconnect(self, close_code):
        """
        Base disconnection handler with cleanup.
        Subclasses should call super().disconnect(close_code) for cleanup.
        """
        try:
            # Leave all channel groups
            for group_name in self.channel_groups:
                await self.channel_layer.group_discard(group_name, self.channel_name)
            
            # Calculate connection duration
            duration = None
            if self.connected_at:
                duration = (timezone.now() - self.connected_at).total_seconds()
            
            logger.info(f"WebSocket disconnected: {self.__class__.__name__} "
                       f"for user {self.user.username if self.user else 'unknown'} "
                       f"in tenant {self.tenant.slug if self.tenant else 'unknown'}, "
                       f"duration: {duration}s, messages: {self.message_count}")
            
        except Exception as e:
            logger.error(f"Error in {self.__class__.__name__} disconnect: {e}")
    
    async def receive(self, text_data=None, bytes_data=None):
        """
        Base message handler with rate limiting and error handling.
        Subclasses should override _handle_message() instead.
        """
        try:
            # Update activity tracking
            self.last_activity = timezone.now()
            self.message_count += 1
            
            # Rate limiting check
            if not await self._check_rate_limit():
                await self._send_error("Rate limit exceeded")
                return
            
            # Parse message
            if text_data:
                try:
                    data = json.loads(text_data)
                except json.JSONDecodeError:
                    await self._send_error("Invalid JSON format")
                    return
            else:
                data = bytes_data
            
            # Handle message in subclass
            await self._handle_message(data)
            
        except Exception as e:
            logger.error(f"Error handling message in {self.__class__.__name__}: {e}")
            await self._send_error("Internal server error")
    
    async def _resolve_tenant_context(self) -> bool:
        """Resolve tenant context from connection scope."""
        try:
            if not self.tenant_id:
                # Try to get tenant from headers or URL parameters
                headers = dict(self.scope.get("headers", []))
                self.tenant_id = headers.get(b"x-tenant-id", b"").decode()
                
                if not self.tenant_id:
                    # Extract from URL if available
                    url_route = self.scope.get("url_route", {})
                    kwargs = url_route.get("kwargs", {})
                    self.tenant_id = kwargs.get("tenant_id")
            
            if self.tenant_id:
                self.tenant = await self._get_tenant_by_id(self.tenant_id)
                return self.tenant is not None
            
            return False
            
        except Exception as e:
            logger.error(f"Error resolving tenant context: {e}")
            return False
    
    @database_sync_to_async
    def _get_tenant_by_id(self, tenant_id: str) -> Optional[Tenant]:
        """Get tenant by ID from database."""
        try:
            return Tenant.objects.get(slug=tenant_id)
        except Tenant.DoesNotExist:
            return None
    
    async def _authenticate_user(self) -> bool:
        """Authenticate user and load user profile."""
        try:
            if not self.user or self.user.is_anonymous:
                return False
            
            # Load user profile
            self.user_profile = await self._get_user_profile()
            if not self.user_profile:
                return False
            
            # Check if user belongs to tenant
            if self.user_profile.tenant != self.tenant:
                return False
            
            # Check if user is active
            if not self.user_profile.is_active:
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error authenticating user: {e}")
            return False
    
    @database_sync_to_async
    def _get_user_profile(self) -> Optional[UserProfile]:
        """Get user profile from database."""
        try:
            return UserProfile.objects.get(user=self.user, tenant=self.tenant)
        except UserProfile.DoesNotExist:
            return None
    
    async def _check_permissions(self) -> bool:
        """
        Check user permissions for WebSocket access.
        Subclasses can override for specific permission checks.
        """
        if not self.user_profile:
            return False
        
        # Base permission check - user must be active
        return self.user_profile.is_active
    
    async def _check_rate_limit(self) -> bool:
        """
        Check rate limiting for message processing.
        Default implementation allows 100 messages per minute.
        """
        # Simple rate limiting - can be enhanced with Redis
        max_messages_per_minute = 100
        time_window = 60  # seconds
        
        if self.connected_at:
            connection_duration = (timezone.now() - self.connected_at).total_seconds()
            if connection_duration > 0:
                rate = self.message_count / (connection_duration / 60)  # messages per minute
                return rate <= max_messages_per_minute
        
        return True
    
    async def _handle_message(self, data: Any):
        """
        Handle incoming message. Subclasses must implement this method.
        """
        raise NotImplementedError("Subclasses must implement _handle_message()")
    
    async def _send_connection_established(self):
        """Send connection established confirmation."""
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'connection_id': self.connection_id,
            'tenant_id': self.tenant.slug if self.tenant else None,
            'user_id': self.user.id if self.user else None,
            'timestamp': timezone.now().isoformat(),
            'consumer_type': self.__class__.__name__
        }))
    
    async def _send_error(self, message: str, error_code: Optional[str] = None):
        """Send error message to client."""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message,
            'error_code': error_code,
            'timestamp': timezone.now().isoformat()
        }))
    
    async def _close_with_error(self, code: int, reason: str):
        """Close connection with error code and reason."""
        logger.warning(f"Closing WebSocket connection: {reason} (code: {code})")
        await self.close(code=code)
    
    async def _join_group(self, group_name: str):
        """Join a channel group with tracking."""
        await self.channel_layer.group_add(group_name, self.channel_name)
        self.channel_groups.add(group_name)
    
    async def _leave_group(self, group_name: str):
        """Leave a channel group with tracking."""
        await self.channel_layer.group_discard(group_name, self.channel_name)
        self.channel_groups.discard(group_name)
    
    def _get_tenant_group_name(self, base_name: str) -> str:
        """Generate tenant-specific group name."""
        return f"{base_name}_{self.tenant.slug}" if self.tenant else base_name


class CallMonitoringConsumer(BaseTenantConsumer):
    """
    Enhanced WebSocket consumer for real-time call monitoring with advanced features:
    - Real-time call metrics (duration, quality, participants)
    - Call recording controls via WebSocket
    - Multi-session monitoring capability
    - Event filtering system
    - Historical replay functionality
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitored_sessions = set()
        self.monitoring_group = None
        self.active_filters = {}
        self.recording_sessions = set()
        self.metrics_interval = 5  # seconds
        self.replay_mode = False
        self.replay_session = None
    
    async def connect(self):
        """Handle WebSocket connection for enhanced call monitoring"""
        # Call base class connect method first
        if not await super().connect():
            return
        
        try:
            # Create tenant-specific monitoring group
            self.monitoring_group = self._get_tenant_group_name("call_monitoring")
            
            # Join monitoring group
            await self._join_group(self.monitoring_group)
            
            # Send current active sessions
            await self._send_active_sessions()
            
            # Start metrics broadcasting
            await self._start_metrics_broadcasting()
            
            logger.info(f"Enhanced call monitoring WebSocket connected for tenant {self.tenant.slug}")
            
        except Exception as e:
            logger.error(f"Error in enhanced call monitoring WebSocket connect: {e}")
            await self._close_with_error(4000, "Connection setup failed")
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection with enhanced cleanup"""
        # Call base class disconnect
        await super().disconnect(close_code)
        
        # Additional cleanup specific to call monitoring
        try:
            # Stop any active recordings
            for session_id in self.recording_sessions.copy():
                await self._stop_recording(session_id)
            
            # Clear monitoring state
            self.monitored_sessions.clear()
            self.recording_sessions.clear()
            self.active_filters.clear()
            
        except Exception as e:
            logger.error(f"Error in call monitoring disconnect cleanup: {e}")
    
    async def _handle_message(self, data):
        """Enhanced message handler for call monitoring features"""
        message_type = data.get('type')
        
        if message_type == 'subscribe_session':
            session_id = data.get('session_id')
            if session_id:
                await self._subscribe_to_session(session_id)
                
        elif message_type == 'unsubscribe_session':
            session_id = data.get('session_id')
            if session_id:
                await self._unsubscribe_from_session(session_id)
                
        elif message_type == 'get_session_details':
            session_id = data.get('session_id')
            if session_id:
                await self._send_session_details(session_id)
                
        elif message_type == 'get_system_status':
            await self._send_system_status()
            
        elif message_type == 'start_recording':
            session_id = data.get('session_id')
            if session_id:
                await self._start_recording(session_id)
                
        elif message_type == 'stop_recording':
            session_id = data.get('session_id')
            if session_id:
                await self._stop_recording(session_id)
                
        elif message_type == 'set_filters':
            filters = data.get('filters', {})
            await self._set_event_filters(filters)
            
        elif message_type == 'get_call_metrics':
            session_id = data.get('session_id')
            if session_id:
                await self._send_call_metrics(session_id)
                
        elif message_type == 'start_multi_monitoring':
            session_ids = data.get('session_ids', [])
            await self._start_multi_monitoring(session_ids)
            
        elif message_type == 'stop_multi_monitoring':
            await self._stop_multi_monitoring()
            
        elif message_type == 'start_replay':
            session_id = data.get('session_id')
            start_time = data.get('start_time')
            end_time = data.get('end_time')
            if session_id:
                await self._start_historical_replay(session_id, start_time, end_time)
                
        elif message_type == 'stop_replay':
            await self._stop_historical_replay()
            
        elif message_type == 'set_metrics_interval':
            interval = data.get('interval', 5)
            await self._set_metrics_interval(interval)
            
        else:
            await self._send_error(f'Unknown message type: {message_type}')
    
    async def session_update(self, event):
        """Handle session update events from channel layer"""
        await self.send(text_data=json.dumps(event['data']))
    
    async def system_alert(self, event):
        """Handle system alert events"""
        await self.send(text_data=json.dumps(event['data']))
    
    async def _authenticate_user(self) -> bool:
        """Authenticate WebSocket user"""
        if isinstance(self.user, AnonymousUser):
            return False
        
        # Additional authentication checks can be added here
        return True
    
    async def _send_active_sessions(self):
        """Send list of active sessions to client"""
        try:
            session_manager = get_session_manager()
            active_sessions = await session_manager.get_active_sessions_for_tenant(self.tenant_id)
            
            sessions_data = []
            for session_id, session_data in active_sessions.items():
                sessions_data.append({
                    'session_id': session_id,
                    'caller_id': session_data.get('caller_id'),
                    'dialed_number': session_data.get('dialed_number'),
                    'call_start_time': session_data.get('call_start_time'),
                    'status': session_data.get('status')
                })
            
            await self.send(text_data=json.dumps({
                'type': 'active_sessions',
                'sessions': sessions_data,
                'count': len(sessions_data),
                'timestamp': timezone.now().isoformat()
            }))
            
        except Exception as e:
            logger.error(f"Error sending active sessions: {e}")
    
    async def _subscribe_to_session(self, session_id: str):
        """Subscribe to specific session updates"""
        try:
            self.monitored_sessions.add(session_id)
            
            # Join session-specific group
            session_group = f"session_{session_id}_{self.tenant_id}"
            await self.channel_layer.group_add(session_group, self.channel_name)
            
            await self.send(text_data=json.dumps({
                'type': 'subscribed',
                'session_id': session_id,
                'message': f'Subscribed to session {session_id}'
            }))
            
        except Exception as e:
            logger.error(f"Error subscribing to session {session_id}: {e}")
    
    async def _unsubscribe_from_session(self, session_id: str):
        """Unsubscribe from specific session updates"""
        try:
            self.monitored_sessions.discard(session_id)
            
            # Leave session-specific group
            session_group = f"session_{session_id}_{self.tenant_id}"
            await self.channel_layer.group_discard(session_group, self.channel_name)
            
            await self.send(text_data=json.dumps({
                'type': 'unsubscribed',
                'session_id': session_id,
                'message': f'Unsubscribed from session {session_id}'
            }))
            
        except Exception as e:
            logger.error(f"Error unsubscribing from session {session_id}: {e}")
    
    async def _send_session_details(self, session_id: str):
        """Send detailed session information"""
        try:
            session_manager = get_session_manager()
            session_data = await session_manager.get_session(session_id)
            
            if not session_data:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Session {session_id} not found'
                }))
                return
            
            # Get additional metrics
            quality_manager = get_quality_manager()
            quality_metrics = quality_manager.get_session_metrics(session_id)
            
            recording_manager = get_recording_manager()
            recordings = await recording_manager.list_session_recordings(session_id)
            
            session_details = {
                'type': 'session_details',
                'session_id': session_id,
                'session_data': session_data,
                'quality_metrics': {
                    'mos_score': quality_metrics.mos_score if quality_metrics else 0,
                    'quality_level': quality_metrics.quality_level.value if quality_metrics else 'unknown'
                },
                'recordings': [r['recording_id'] for r in recordings],
                'timestamp': timezone.now().isoformat()
            }
            
            await self.send(text_data=json.dumps(session_details))
            
        except Exception as e:
            logger.error(f"Error sending session details for {session_id}: {e}")
    
    async def _send_system_status(self):
        """Send system status information"""
        try:
            session_manager = get_session_manager()
            quality_manager = get_quality_manager()
            recording_manager = get_recording_manager()
            streaming_manager = get_streaming_manager()
            
            status_data = {
                'type': 'system_status',
                'session_stats': session_manager.get_statistics(),
                'quality_stats': quality_manager.get_system_quality_report(),
                'recording_stats': recording_manager.get_comprehensive_stats(),
                'streaming_stats': streaming_manager.get_comprehensive_stats(),
                'timestamp': timezone.now().isoformat()
            }
            
            await self.send(text_data=json.dumps(status_data))
            
        except Exception as e:
            logger.error(f"Error sending system status: {e}")


class LiveAudioStreamConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for live audio streaming"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.tenant_id = None
        self.session_id = None
        self.streaming_connection = None
    
    async def connect(self):
        """Handle WebSocket connection for audio streaming"""
        try:
            # Get parameters from URL
            self.session_id = self.scope['url_route']['kwargs'].get('session_id')
            self.tenant_id = self.scope.get('tenant_id', 'default')
            self.user = self.scope.get("user", AnonymousUser())
            
            if not self.session_id:
                await self.close(code=4400)  # Bad request
                return
            
            # Check authentication
            if not await self._authenticate_user():
                await self.close(code=4001)  # Authentication failed
                return
            
            # Accept connection
            await self.accept()
            
            # Create streaming connection
            await self._setup_audio_streaming()
            
            logger.info(f"Live audio streaming connected for session {self.session_id}")
            
        except Exception as e:
            logger.error(f"Error in live audio streaming connect: {e}")
            await self.close(code=4000)
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        try:
            # Clean up streaming connection
            if self.streaming_connection:
                streaming_manager = get_streaming_manager()
                await streaming_manager.websocket_streamer.remove_connection(
                    self.streaming_connection.connection_id
                )
            
            logger.info(f"Live audio streaming disconnected for session {self.session_id}")
            
        except Exception as e:
            logger.error(f"Error in live audio streaming disconnect: {e}")
    
    async def receive(self, text_data=None, bytes_data=None):
        """Handle incoming WebSocket messages"""
        try:
            if text_data:
                data = json.loads(text_data)
                command = data.get('command')
                
                if command == 'change_quality':
                    quality = data.get('quality', 'medium')
                    await self._change_stream_quality(quality)
                elif command == 'get_stats':
                    await self._send_streaming_stats()
                    
        except Exception as e:
            logger.error(f"Error handling live audio streaming message: {e}")
    
    async def _authenticate_user(self) -> bool:
        """Authenticate WebSocket user for audio streaming"""
        if isinstance(self.user, AnonymousUser):
            return False
        
        # Check if user has permission to access this session
        # Additional checks can be added here
        return True
    
    async def _setup_audio_streaming(self):
        """Setup audio streaming connection"""
        try:
            from core.junie_codes.audio.audio_streaming import StreamConnection, StreamingConfig
            import time
            
            config = StreamingConfig(
                protocol=StreamingProtocol.WEBSOCKET,
                format=StreamingFormat.JSON_WRAPPED,
                quality=StreamQuality.MEDIUM
            )
            
            self.streaming_connection = StreamConnection(
                connection_id=f"live_{self.session_id}_{int(time.time())}",
                session_id=self.session_id,
                tenant_id=self.tenant_id,
                protocol=StreamingProtocol.WEBSOCKET,
                config=config,
                created_at=timezone.now(),
                last_activity=timezone.now(),
                websocket_consumer=self
            )
            
            # Register with streaming manager
            streaming_manager = get_streaming_manager()
            await streaming_manager.add_streaming_connection(self.streaming_connection)
            
        except Exception as e:
            logger.error(f"Error setting up audio streaming: {e}")
    
    async def _change_stream_quality(self, quality: str):
        """Change streaming quality"""
        try:
            if self.streaming_connection:
                quality_enum = StreamQuality(quality)
                self.streaming_connection.config.quality = quality_enum
                
                await self.send(text_data=json.dumps({
                    'type': 'quality_changed',
                    'quality': quality,
                    'message': f'Stream quality changed to {quality}'
                }))
                
        except Exception as e:
            logger.error(f"Error changing stream quality: {e}")
    
    async def _send_streaming_stats(self):
        """Send streaming statistics"""
        try:
            streaming_manager = get_streaming_manager()
            stats = streaming_manager.get_comprehensive_stats()
            
            await self.send(text_data=json.dumps({
                'type': 'streaming_stats',
                'data': stats,
                'timestamp': timezone.now().isoformat()
            }))
            
        except Exception as e:
            logger.error(f"Error sending streaming stats: {e}")


class SystemStatusConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for system status updates"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.tenant_id = None
        self.status_group = None
    
    async def connect(self):
        """Handle WebSocket connection for system status"""
        try:
            self.user = self.scope.get("user", AnonymousUser())
            self.tenant_id = self.scope.get("tenant_id", "default")
            
            # Check authentication
            if not await self._authenticate_user():
                await self.close(code=4001)
                return
            
            # Create status group
            self.status_group = f"system_status_{self.tenant_id}"
            await self.channel_layer.group_add(self.status_group, self.channel_name)
            
            # Accept connection
            await self.accept()
            
            # Send initial status
            await self._send_system_status()
            
            logger.info(f"System status WebSocket connected for tenant {self.tenant_id}")
            
        except Exception as e:
            logger.error(f"Error in system status WebSocket connect: {e}")
            await self.close(code=4000)
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        try:
            if self.status_group:
                await self.channel_layer.group_discard(self.status_group, self.channel_name)
            
            logger.info(f"System status WebSocket disconnected for tenant {self.tenant_id}")
            
        except Exception as e:
            logger.error(f"Error in system status WebSocket disconnect: {e}")
    
    async def receive(self, text_data):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(text_data)
            command = data.get('command')
            
            if command == 'get_status':
                await self._send_system_status()
            elif command == 'get_health':
                await self._send_health_check()
                
        except Exception as e:
            logger.error(f"Error handling system status message: {e}")
    
    async def status_update(self, event):
        """Handle status update events from channel layer"""
        await self.send(text_data=json.dumps(event['data']))
    
    async def _authenticate_user(self) -> bool:
        """Authenticate WebSocket user for system status"""
        if isinstance(self.user, AnonymousUser):
            return False
        
        # Check if user has admin privileges
        # Additional checks can be added here
        return True
    
    async def _send_system_status(self):
        """Send comprehensive system status"""
        try:
            session_manager = get_session_manager()
            quality_manager = get_quality_manager()
            recording_manager = get_recording_manager()
            streaming_manager = get_streaming_manager()
            
            status_data = {
                'type': 'system_status',
                'tenant_id': self.tenant_id,
                'session_manager': session_manager.get_statistics(),
                'quality_manager': quality_manager.get_system_quality_report(),
                'recording_manager': recording_manager.get_comprehensive_stats(),
                'streaming_manager': streaming_manager.get_comprehensive_stats(),
                'timestamp': timezone.now().isoformat()
            }
            
            await self.send(text_data=json.dumps(status_data))
            
        except Exception as e:
            logger.error(f"Error sending system status: {e}")
    
    async def _send_health_check(self):
        """Send system health check"""
        try:
            health_data = {
                'type': 'health_check',
                'status': 'healthy',
                'components': {
                    'database': 'ok',
                    'redis': 'ok',
                    'rtp_server': 'ok',
                    'session_manager': 'ok'
                },
                'timestamp': timezone.now().isoformat()
            }
            
            await self.send(text_data=json.dumps(health_data))
            
        except Exception as e:
            logger.error(f"Error sending health check: {e}")


class AdminNotificationConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for admin notifications"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.tenant_id = None
        self.admin_group = None
    
    async def connect(self):
        """Handle WebSocket connection for admin notifications"""
        try:
            self.user = self.scope.get("user", AnonymousUser())
            self.tenant_id = self.scope.get("tenant_id", "default")
            
            # Check admin authentication
            if not await self._authenticate_admin():
                await self.close(code=4003)  # Forbidden
                return
            
            # Create admin group
            self.admin_group = f"admin_notifications_{self.tenant_id}"
            await self.channel_layer.group_add(self.admin_group, self.channel_name)
            
            # Accept connection
            await self.accept()
            
            # Send welcome message
            await self.send(text_data=json.dumps({
                'type': 'connected',
                'message': 'Admin notifications connected',
                'tenant_id': self.tenant_id,
                'timestamp': timezone.now().isoformat()
            }))
            
            logger.info(f"Admin notifications WebSocket connected for tenant {self.tenant_id}")
            
        except Exception as e:
            logger.error(f"Error in admin notifications WebSocket connect: {e}")
            await self.close(code=4000)
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        try:
            if self.admin_group:
                await self.channel_layer.group_discard(self.admin_group, self.channel_name)
            
            logger.info(f"Admin notifications WebSocket disconnected for tenant {self.tenant_id}")
            
        except Exception as e:
            logger.error(f"Error in admin notifications WebSocket disconnect: {e}")
    
    async def receive(self, text_data):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(text_data)
            command = data.get('command')
            
            if command == 'get_alerts':
                await self._send_active_alerts()
            elif command == 'acknowledge_alert':
                alert_id = data.get('alert_id')
                if alert_id:
                    await self._acknowledge_alert(alert_id)
                    
        except Exception as e:
            logger.error(f"Error handling admin notification message: {e}")
    
    async def admin_alert(self, event):
        """Handle admin alert events from channel layer"""
        await self.send(text_data=json.dumps(event['data']))
    
    async def system_notification(self, event):
        """Handle system notification events"""
        await self.send(text_data=json.dumps(event['data']))
    
    async def _authenticate_admin(self) -> bool:
        """Authenticate WebSocket user for admin access"""
        if isinstance(self.user, AnonymousUser):
            return False
        
        # Check if user has admin privileges
        # This would typically check user roles/permissions
        return self.user.is_staff or self.user.is_superuser
    
    async def _send_active_alerts(self):
        """Send list of active system alerts"""
        try:
            # This would fetch active alerts from database or cache
            alerts = [
                {
                    'alert_id': 'alert_001',
                    'type': 'quality_degradation',
                    'severity': 'warning',
                    'message': 'Audio quality below threshold for session xyz',
                    'timestamp': timezone.now().isoformat()
                }
            ]
            
            await self.send(text_data=json.dumps({
                'type': 'active_alerts',
                'alerts': alerts,
                'count': len(alerts),
                'timestamp': timezone.now().isoformat()
            }))
            
        except Exception as e:
            logger.error(f"Error sending active alerts: {e}")
    
    async def _acknowledge_alert(self, alert_id: str):
        """Acknowledge an alert"""
        try:
            # This would mark alert as acknowledged in database
            await self.send(text_data=json.dumps({
                'type': 'alert_acknowledged',
                'alert_id': alert_id,
                'acknowledged_by': self.user.username if self.user else 'unknown',
                'timestamp': timezone.now().isoformat()
            }))
            
        except Exception as e:
            logger.error(f"Error acknowledging alert {alert_id}: {e}")


# Consumer routing mapping
websocket_consumers = {
    'call_monitoring': CallMonitoringConsumer,
    'live_audio': LiveAudioStreamConsumer, 
    'system_status': SystemStatusConsumer,
    'admin_notifications': AdminNotificationConsumer,
}
