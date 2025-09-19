"""
Session Lifecycle Management for aiMediaGateway
Handles session creation from AMI events, state tracking, and event notifications.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .session_manager import SessionManager, CallSessionData, get_session_manager
from .models import CallSession, Tenant


logger = logging.getLogger(__name__)


@dataclass
class AMIEvent:
    """AMI Event data structure"""
    event_type: str
    channel: str
    unique_id: str
    caller_id_num: Optional[str] = None
    caller_id_name: Optional[str] = None
    exten: Optional[str] = None
    context: Optional[str] = None
    priority: Optional[str] = None
    application: Optional[str] = None
    data: Optional[str] = None
    timestamp: Optional[datetime] = None
    raw_data: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = timezone.now()
        if self.raw_data is None:
            self.raw_data = {}


class SessionLifecycleManager:
    """
    Manages the complete lifecycle of call sessions from AMI event detection
    through session cleanup and database persistence.
    """

    def __init__(self, session_manager: Optional[SessionManager] = None):
        self.session_manager = session_manager or get_session_manager()
        self.channel_layer = get_channel_layer()
        self.event_handlers = {}
        self._setup_event_handlers()

    def _setup_event_handlers(self):
        """Set up AMI event handlers for session lifecycle"""
        self.event_handlers = {
            'Newchannel': self._handle_new_channel,
            'Dial': self._handle_dial,
            'DialEnd': self._handle_dial_end,
            'Bridge': self._handle_bridge,
            'BridgeLeave': self._handle_bridge_leave,
            'Hangup': self._handle_hangup,
            'VarSet': self._handle_var_set,
        }

    async def process_ami_event(self, ami_event: AMIEvent, tenant_id: str) -> bool:
        """
        Process incoming AMI event and update session state accordingly
        """
        try:
            event_handler = self.event_handlers.get(ami_event.event_type)
            if event_handler:
                await event_handler(ami_event, tenant_id)
                return True
            else:
                logger.debug(f"No handler for AMI event: {ami_event.event_type}")
                return False

        except Exception as e:
            logger.error(f"Error processing AMI event {ami_event.event_type}: {e}")
            return False

    async def _handle_new_channel(self, ami_event: AMIEvent, tenant_id: str):
        """Handle Newchannel event - early call detection"""
        try:
            # Check if session already exists
            existing_session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if existing_session:
                logger.debug(f"Session already exists for channel {ami_event.channel}")
                return

            # Create new session data
            session_data = CallSessionData(
                session_id="",  # Will be generated
                tenant_id=tenant_id,
                asterisk_channel_id=ami_event.channel,
                asterisk_unique_id=ami_event.unique_id,
                caller_id_name=ami_event.caller_id_name,
                caller_id_number=ami_event.caller_id_num,
                dialed_number=ami_event.exten,
                direction='inbound' if ami_event.context in ['from-pstn', 'from-internal'] else 'outbound',
                status='detected',
                call_start_time=ami_event.timestamp,
                session_metadata={
                    'context': ami_event.context,
                    'priority': ami_event.priority,
                    'application': ami_event.application,
                    'ami_data': ami_event.raw_data
                }
            )

            # Create session in Redis
            session_id = await self.session_manager.create_session(session_data)
            
            # Notify WebSocket clients
            await self._notify_session_event('session_created', session_data, tenant_id)
            
            logger.info(f"Created session {session_id} for new channel {ami_event.channel}")

        except Exception as e:
            logger.error(f"Error handling Newchannel event: {e}")

    async def _handle_dial(self, ami_event: AMIEvent, tenant_id: str):
        """Handle Dial event - outbound call detection"""
        try:
            session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if session:
                updates = {
                    'status': 'dialing',
                    'session_metadata': {
                        **session.session_metadata,
                        'dial_destination': ami_event.data
                    }
                }
                await self.session_manager.update_session(session.session_id, updates)
                await self._notify_session_event('session_updated', session, tenant_id)

        except Exception as e:
            logger.error(f"Error handling Dial event: {e}")

    async def _handle_dial_end(self, ami_event: AMIEvent, tenant_id: str):
        """Handle DialEnd event - call answer or failure"""
        try:
            session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if session:
                # Determine if call was answered based on dial status
                dial_status = ami_event.raw_data.get('DialStatus', 'UNKNOWN')
                
                if dial_status == 'ANSWER':
                    updates = {
                        'status': 'answered',
                        'call_answer_time': ami_event.timestamp
                    }
                else:
                    updates = {
                        'status': 'failed',
                        'call_end_time': ami_event.timestamp,
                        'session_metadata': {
                            **session.session_metadata,
                            'dial_status': dial_status
                        }
                    }
                
                await self.session_manager.update_session(session.session_id, updates)
                await self._notify_session_event('session_updated', session, tenant_id)

        except Exception as e:
            logger.error(f"Error handling DialEnd event: {e}")

    async def _handle_bridge(self, ami_event: AMIEvent, tenant_id: str):
        """Handle Bridge event - call bridging"""
        try:
            session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if session:
                bridge_id = ami_event.raw_data.get('BridgeUniqueid')
                updates = {
                    'status': 'bridged',
                    'bridge_id': bridge_id,
                    'session_metadata': {
                        **session.session_metadata,
                        'bridge_type': ami_event.raw_data.get('BridgeType', 'unknown')
                    }
                }
                await self.session_manager.update_session(session.session_id, updates)
                await self._notify_session_event('session_updated', session, tenant_id)

        except Exception as e:
            logger.error(f"Error handling Bridge event: {e}")

    async def _handle_bridge_leave(self, ami_event: AMIEvent, tenant_id: str):
        """Handle BridgeLeave event - call unbridging"""
        try:
            session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if session:
                updates = {
                    'status': 'unbridged',
                    'bridge_id': None
                }
                await self.session_manager.update_session(session.session_id, updates)
                await self._notify_session_event('session_updated', session, tenant_id)

        except Exception as e:
            logger.error(f"Error handling BridgeLeave event: {e}")

    async def _handle_hangup(self, ami_event: AMIEvent, tenant_id: str):
        """Handle Hangup event - call termination"""
        try:
            session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if session:
                hangup_cause = ami_event.raw_data.get('Cause', 'UNKNOWN')
                hangup_cause_txt = ami_event.raw_data.get('Cause-txt', 'Unknown')
                
                updates = {
                    'status': 'ended',
                    'call_end_time': ami_event.timestamp,
                    'session_metadata': {
                        **session.session_metadata,
                        'hangup_cause': hangup_cause,
                        'hangup_cause_txt': hangup_cause_txt
                    }
                }
                
                await self.session_manager.update_session(session.session_id, updates)
                await self._notify_session_event('session_ended', session, tenant_id)
                
                # Persist to database
                await self._persist_session_to_db(session)
                
                logger.info(f"Session {session.session_id} ended with cause: {hangup_cause_txt}")

        except Exception as e:
            logger.error(f"Error handling Hangup event: {e}")

    async def _handle_var_set(self, ami_event: AMIEvent, tenant_id: str):
        """Handle VarSet event - custom variable updates"""
        try:
            session = await self.session_manager.get_session_by_channel(ami_event.channel)
            if session:
                var_name = ami_event.raw_data.get('Variable')
                var_value = ami_event.raw_data.get('Value')
                
                # Update session metadata with custom variables
                if var_name and var_value:
                    session_metadata = session.session_metadata.copy()
                    session_metadata.setdefault('custom_vars', {})[var_name] = var_value
                    
                    updates = {'session_metadata': session_metadata}
                    await self.session_manager.update_session(session.session_id, updates)

        except Exception as e:
            logger.error(f"Error handling VarSet event: {e}")

    async def _notify_session_event(self, event_type: str, session: CallSessionData, tenant_id: str):
        """Send session event notification via WebSocket"""
        try:
            if not self.channel_layer:
                return

            # Create notification data
            notification_data = {
                'type': 'session_event',
                'event': event_type,
                'session_id': session.session_id,
                'tenant_id': tenant_id,
                'data': {
                    'caller_id_number': session.caller_id_number,
                    'dialed_number': session.dialed_number,
                    'status': session.status,
                    'direction': session.direction,
                    'timestamp': session.updated_at.isoformat() if session.updated_at else None
                }
            }

            # Send to tenant-specific group
            await self.channel_layer.group_send(
                f"tenant_{tenant_id}",
                {
                    'type': 'session_notification',
                    'data': notification_data
                }
            )

        except Exception as e:
            logger.error(f"Error sending session notification: {e}")

    async def _persist_session_to_db(self, session: CallSessionData):
        """Persist session data to PostgreSQL database"""
        try:
            # Get tenant object
            tenant = await Tenant.objects.aget(id=session.tenant_id)
            
            # Create CallSession record
            call_session = CallSession(
                id=session.session_id,
                tenant=tenant,
                asterisk_channel_id=session.asterisk_channel_id,
                asterisk_unique_id=session.asterisk_unique_id,
                caller_id_name=session.caller_id_name,
                caller_id_number=session.caller_id_number,
                dialed_number=session.dialed_number,
                direction=session.direction,
                status=session.status,
                bridge_id=session.bridge_id,
                external_media_channel_id=session.external_media_channel_id,
                rtp_endpoint_host=session.rtp_endpoint_host,
                rtp_endpoint_port=session.rtp_endpoint_port,
                session_metadata=session.session_metadata or {},
                call_start_time=session.call_start_time,
                call_answer_time=session.call_answer_time,
                call_end_time=session.call_end_time
            )
            
            await call_session.asave()
            logger.info(f"Persisted session {session.session_id} to database")

        except Exception as e:
            logger.error(f"Error persisting session to database: {e}")

    async def cleanup_expired_sessions(self, max_age_hours: int = 24):
        """Clean up expired sessions from Redis and optionally database"""
        try:
            expired_count = await self.session_manager.expire_old_sessions(max_age_hours)
            logger.info(f"Cleaned up {expired_count} expired sessions")
            return expired_count

        except Exception as e:
            logger.error(f"Error cleaning up expired sessions: {e}")
            return 0

    async def get_active_sessions_count(self, tenant_id: Optional[str] = None) -> int:
        """Get count of active sessions for monitoring"""
        try:
            stats = await self.session_manager.get_session_stats(tenant_id)
            return stats.get('active_sessions', 0)

        except Exception as e:
            logger.error(f"Error getting active sessions count: {e}")
            return 0


# Singleton instance
_lifecycle_manager = None

def get_lifecycle_manager() -> SessionLifecycleManager:
    """Get singleton SessionLifecycleManager instance"""
    global _lifecycle_manager
    if _lifecycle_manager is None:
        _lifecycle_manager = SessionLifecycleManager()
    return _lifecycle_manager
