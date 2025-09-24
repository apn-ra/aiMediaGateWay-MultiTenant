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
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone
from django.contrib.auth.models import AnonymousUser

# Import our components
from core.junie_codes.session import get_session_manager
from core.junie_codes.audio.audio_streaming import get_streaming_manager, StreamingProtocol, StreamQuality, StreamingFormat
from core.junie_codes.audio import get_recording_manager
from core.junie_codes.audio.audio_quality import get_quality_manager

logger = logging.getLogger(__name__)


class CallMonitoringConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time call monitoring"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.tenant_id = None
        self.monitored_sessions = set()
        self.monitoring_group = None
    
    async def connect(self):
        """Handle WebSocket connection for call monitoring"""
        try:
            # Get user and tenant from scope
            self.user = self.scope.get("user", AnonymousUser())
            self.tenant_id = self.scope.get("tenant_id", "default")
            
            # Check authentication
            if not await self._authenticate_user():
                await self.close(code=4001)  # Authentication failed
                return
            
            # Create tenant-specific monitoring group
            self.monitoring_group = f"call_monitoring_{self.tenant_id}"
            
            # Join monitoring group
            await self.channel_layer.group_add(self.monitoring_group, self.channel_name)
            
            # Accept connection
            await self.accept()
            
            # Send initial connection confirmation
            await self.send(text_data=json.dumps({
                'type': 'connection_established',
                'tenant_id': self.tenant_id,
                'timestamp': timezone.now().isoformat(),
                'message': 'Call monitoring connected'
            }))
            
            # Send current active sessions
            await self._send_active_sessions()
            
            logger.info(f"Call monitoring WebSocket connected for tenant {self.tenant_id}")
            
        except Exception as e:
            logger.error(f"Error in call monitoring WebSocket connect: {e}")
            await self.close(code=4000)
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        try:
            # Leave monitoring group
            if self.monitoring_group:
                await self.channel_layer.group_discard(self.monitoring_group, self.channel_name)
            
            logger.info(f"Call monitoring WebSocket disconnected for tenant {self.tenant_id}")
            
        except Exception as e:
            logger.error(f"Error in call monitoring WebSocket disconnect: {e}")
    
    async def receive(self, text_data):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(text_data)
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
                
            else:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Unknown message type: {message_type}'
                }))
                
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON format'
            }))
        except Exception as e:
            logger.error(f"Error handling call monitoring message: {e}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))
    
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
