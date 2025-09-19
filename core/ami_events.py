"""
AMI Event Handlers

This module provides event handlers for Asterisk Manager Interface (AMI) events
using Panoramisk. It implements early call detection, session management,
and multi-tenant event routing.
"""

import asyncio
import logging
from typing import Dict, Any, Optional, Callable
from datetime import datetime
from django.utils import timezone
from django.conf import settings

from .models import Tenant, CallSession
from .session_manager import CallSessionData, get_session_manager
from .ami_manager import get_ami_manager


logger = logging.getLogger(__name__)


class AMIEventHandler:
    """Handler for AMI events with multi-tenant support."""
    
    def __init__(self):
        self.session_manager = None
        self.ami_manager = None
        self._tenant_cache = {}
        self._event_statistics = {
            'newchannel': 0,
            'dial': 0,
            'hangup': 0,
            'bridge': 0,
            'varset': 0,
            'total': 0
        }
    
    async def initialize(self):
        """Initialize the event handler with managers."""
        self.session_manager = get_session_manager()
        self.ami_manager = await get_ami_manager()
        
    async def register_handlers(self, tenant_id: str):
        """Register all event handlers for a specific tenant."""
        if not self.ami_manager:
            await self.initialize()
            
        # Register event handlers with the AMI manager
        await self.ami_manager.register_event_handler(
            tenant_id, 'Newchannel', self.handle_newchannel
        )
        await self.ami_manager.register_event_handler(
            tenant_id, 'Dial', self.handle_dial
        )
        await self.ami_manager.register_event_handler(
            tenant_id, 'Hangup', self.handle_hangup
        )
        await self.ami_manager.register_event_handler(
            tenant_id, 'Bridge', self.handle_bridge
        )
        await self.ami_manager.register_event_handler(
            tenant_id, 'VarSet', self.handle_varset
        )
        
        logger.info(f"Registered AMI event handlers for tenant {tenant_id}")
    
    async def handle_newchannel(self, manager, message: Dict[str, Any]):
        """
        Handle Newchannel events for early call detection.
        
        This is triggered when a new channel is created in Asterisk,
        allowing us to create sessions before the call is answered.
        """
        try:
            # Extract channel information
            channel = message.get('Channel', '')
            caller_id_num = message.get('CallerIDNum', '')
            caller_id_name = message.get('CallerIDName', '')
            context = message.get('Context', '')
            exten = message.get('Exten', '')
            uniqueid = message.get('Uniqueid', '')
            
            logger.info(f"Newchannel event: {channel} from {caller_id_num}")
            
            # Determine tenant from channel or context
            tenant_id = await self._extract_tenant_from_context(context, channel)
            if not tenant_id:
                logger.warning(f"Could not determine tenant for channel {channel}")
                return
            
            # Create session data
            session_data = CallSessionData(
                session_id=uniqueid,
                tenant_id=tenant_id,
                channel_id=channel,
                caller_id=caller_id_num,
                caller_name=caller_id_name,
                dialed_number=exten,
                call_direction='inbound' if self._is_inbound_channel(channel) else 'outbound',
                status='ringing',
                start_time=timezone.now(),
                context=context,
                metadata={
                    'ami_event': 'Newchannel',
                    'uniqueid': uniqueid,
                    'original_message': message
                }
            )
            
            # Create session in Redis
            success = await self.session_manager.create_session(session_data)
            if success:
                logger.info(f"Created session {uniqueid} for channel {channel}")
                self._event_statistics['newchannel'] += 1
                
                # Optionally create database record for persistent storage
                await self._create_database_session(session_data)
            else:
                logger.error(f"Failed to create session for channel {channel}")
                
        except Exception as e:
            logger.error(f"Error handling Newchannel event: {e}")
            logger.error(f"Message: {message}")
    
    async def handle_dial(self, manager, message: Dict[str, Any]):
        """
        Handle Dial events for outbound call tracking.
        
        This tracks when a channel starts dialing another channel.
        """
        try:
            # Extract dial information
            source = message.get('Channel', '')
            destination = message.get('Destination', '')
            caller_id_num = message.get('CallerIDNum', '')
            dial_string = message.get('DialString', '')
            uniqueid = message.get('Uniqueid', '')
            dest_uniqueid = message.get('DestUniqueID', '')
            
            logger.info(f"Dial event: {source} dialing {destination}")
            
            # Update existing session or create new one
            session = await self.session_manager.get_session(uniqueid)
            if session:
                # Update existing session with dial information
                updates = {
                    'status': 'dialing',
                    'destination_channel': destination,
                    'dial_string': dial_string,
                    'destination_uniqueid': dest_uniqueid,
                    'metadata': {
                        **session.metadata,
                        'dial_event': message
                    }
                }
                await self.session_manager.update_session(uniqueid, updates)
                logger.info(f"Updated session {uniqueid} with dial information")
            else:
                # Create new session for outbound call
                tenant_id = await self._extract_tenant_from_context(
                    message.get('Context', ''), source
                )
                if tenant_id:
                    session_data = CallSessionData(
                        session_id=uniqueid,
                        tenant_id=tenant_id,
                        channel_id=source,
                        caller_id=caller_id_num,
                        dialed_number=dial_string,
                        call_direction='outbound',
                        status='dialing',
                        start_time=timezone.now(),
                        destination_channel=destination,
                        metadata={
                            'ami_event': 'Dial',
                            'dial_string': dial_string,
                            'dest_uniqueid': dest_uniqueid,
                            'original_message': message
                        }
                    )
                    await self.session_manager.create_session(session_data)
                    logger.info(f"Created outbound session {uniqueid}")
                    
                    await self._create_database_session(session_data)
            
            self._event_statistics['dial'] += 1
            
        except Exception as e:
            logger.error(f"Error handling Dial event: {e}")
            logger.error(f"Message: {message}")
    
    async def handle_hangup(self, manager, message: Dict[str, Any]):
        """
        Handle Hangup events for session cleanup.
        
        This cleans up sessions when calls end.
        """
        try:
            # Extract hangup information
            channel = message.get('Channel', '')
            uniqueid = message.get('Uniqueid', '')
            cause = message.get('Cause', '')
            cause_txt = message.get('Cause-txt', '')
            
            logger.info(f"Hangup event: {channel} (cause: {cause_txt})")
            
            # Get existing session
            session = await self.session_manager.get_session(uniqueid)
            if session:
                # Update session with hangup information
                end_time = timezone.now()
                duration = None
                if session.start_time:
                    duration = (end_time - session.start_time).total_seconds()
                
                updates = {
                    'status': 'completed',
                    'end_time': end_time,
                    'duration': duration,
                    'hangup_cause': cause,
                    'hangup_cause_txt': cause_txt,
                    'metadata': {
                        **session.metadata,
                        'hangup_event': message
                    }
                }
                await self.session_manager.update_session(uniqueid, updates)
                
                # Update database record
                await self._update_database_session(session, updates)
                
                logger.info(f"Updated session {uniqueid} with hangup information")
                
                # Schedule session cleanup after a delay
                asyncio.create_task(self._delayed_session_cleanup(uniqueid, delay=300))  # 5 minutes
            else:
                logger.warning(f"No session found for hangup event: {channel}")
            
            self._event_statistics['hangup'] += 1
            
        except Exception as e:
            logger.error(f"Error handling Hangup event: {e}")
            logger.error(f"Message: {message}")
    
    async def handle_bridge(self, manager, message: Dict[str, Any]):
        """
        Handle Bridge events for call bridging.
        
        This tracks when channels are bridged together.
        """
        try:
            # Extract bridge information
            bridge_uniqueid = message.get('BridgeUniqueID', '')
            bridge_type = message.get('BridgeType', '')
            bridge_technology = message.get('BridgeTechnology', '')
            bridge_creator = message.get('BridgeCreator', '')
            bridge_name = message.get('BridgeName', '')
            bridge_num_channels = message.get('BridgeNumChannels', '0')
            
            logger.info(f"Bridge event: {bridge_uniqueid} ({bridge_type})")
            
            # Find sessions that might be involved in this bridge
            # This is a simplified approach - in production, you'd track individual
            # channel bridge events to get specific channel information
            
            # For now, we'll add bridge information to sessions based on timing
            # and update their metadata
            recent_sessions = await self._get_recent_active_sessions()
            
            for session in recent_sessions:
                if session.status in ['ringing', 'dialing', 'answered']:
                    updates = {
                        'status': 'bridged' if bridge_type == 'basic' else session.status,
                        'bridge_id': bridge_uniqueid,
                        'metadata': {
                            **session.metadata,
                            'bridge_event': message,
                            'bridge_type': bridge_type,
                            'bridge_technology': bridge_technology
                        }
                    }
                    await self.session_manager.update_session(session.session_id, updates)
                    
                    # Update database record
                    await self._update_database_session_by_id(session.session_id, updates)
            
            self._event_statistics['bridge'] += 1
            
        except Exception as e:
            logger.error(f"Error handling Bridge event: {e}")
            logger.error(f"Message: {message}")
    
    async def handle_varset(self, manager, message: Dict[str, Any]):
        """
        Handle VarSet events for custom variables.
        
        This tracks custom channel variables that might contain
        tenant-specific or application-specific data.
        """
        try:
            # Extract variable information
            channel = message.get('Channel', '')
            variable = message.get('Variable', '')
            value = message.get('Value', '')
            uniqueid = message.get('Uniqueid', '')
            
            # Only process variables we care about
            interesting_variables = [
                'TENANT_ID', 'CUSTOMER_ID', 'CALL_TYPE', 'RECORDING_REQUIRED',
                'QUEUE_NAME', 'AGENT_ID', 'CAMPAIGN_ID', 'CALLER_PRIORITY'
            ]
            
            if variable in interesting_variables:
                logger.info(f"VarSet event: {channel} {variable}={value}")
                
                # Update session with custom variable
                session = await self.session_manager.get_session(uniqueid)
                if session:
                    # Add variable to session metadata
                    custom_vars = session.metadata.get('custom_variables', {})
                    custom_vars[variable] = value
                    
                    updates = {
                        'metadata': {
                            **session.metadata,
                            'custom_variables': custom_vars,
                            'varset_events': session.metadata.get('varset_events', []) + [message]
                        }
                    }
                    
                    # Special handling for TENANT_ID variable
                    if variable == 'TENANT_ID' and value:
                        updates['tenant_id'] = value
                    
                    await self.session_manager.update_session(uniqueid, updates)
                    logger.info(f"Updated session {uniqueid} with variable {variable}")
                    
                    # Update database record
                    await self._update_database_session_by_id(uniqueid, updates)
            
            self._event_statistics['varset'] += 1
            
        except Exception as e:
            logger.error(f"Error handling VarSet event: {e}")
            logger.error(f"Message: {message}")
    
    # Helper methods
    
    async def _extract_tenant_from_context(self, context: str, channel: str) -> Optional[str]:
        """Extract tenant ID from context or channel information."""
        try:
            # Try to extract from context (e.g., "tenant_123_inbound")
            if context and 'tenant_' in context:
                parts = context.split('_')
                for i, part in enumerate(parts):
                    if part == 'tenant' and i + 1 < len(parts):
                        tenant_id = parts[i + 1]
                        # Validate tenant exists
                        if await self._validate_tenant(tenant_id):
                            return tenant_id
            
            # Try to extract from channel technology (e.g., "PJSIP/tenant123-00000001")
            if channel and '/' in channel:
                tech, rest = channel.split('/', 1)
                if 'tenant' in rest.lower():
                    # Extract tenant ID from channel name
                    import re
                    match = re.search(r'tenant(\d+)', rest, re.IGNORECASE)
                    if match:
                        tenant_id = match.group(1)
                        if await self._validate_tenant(tenant_id):
                            return tenant_id
            
            # Default to first active tenant if no specific tenant found
            # This is a fallback for single-tenant scenarios
            if not hasattr(self, '_default_tenant'):
                self._default_tenant = await self._get_default_tenant()
            
            return self._default_tenant
            
        except Exception as e:
            logger.error(f"Error extracting tenant from context {context}, channel {channel}: {e}")
            return None
    
    async def _validate_tenant(self, tenant_id: str) -> bool:
        """Validate that a tenant exists and is active."""
        try:
            if tenant_id in self._tenant_cache:
                return self._tenant_cache[tenant_id]
            
            # Check database
            tenant_exists = await asyncio.to_thread(
                lambda: Tenant.objects.filter(
                    id=tenant_id, 
                    is_active=True
                ).exists()
            )
            
            self._tenant_cache[tenant_id] = tenant_exists
            return tenant_exists
            
        except Exception as e:
            logger.error(f"Error validating tenant {tenant_id}: {e}")
            return False
    
    async def _get_default_tenant(self) -> Optional[str]:
        """Get the default tenant for single-tenant scenarios."""
        try:
            tenant = await asyncio.to_thread(
                lambda: Tenant.objects.filter(is_active=True).first()
            )
            return str(tenant.id) if tenant else None
        except Exception as e:
            logger.error(f"Error getting default tenant: {e}")
            return None
    
    def _is_inbound_channel(self, channel: str) -> bool:
        """Determine if a channel represents an inbound call."""
        # This is a simplified heuristic - in production, you'd use
        # more sophisticated logic based on your Asterisk configuration
        inbound_technologies = ['PJSIP', 'SIP', 'DAHDI', 'IAX2']
        outbound_technologies = ['Local']
        
        if '/' in channel:
            tech = channel.split('/')[0]
            if tech in outbound_technologies:
                return False
            if tech in inbound_technologies:
                return True
        
        # Default to inbound for unknown channels
        return True
    
    async def _create_database_session(self, session_data: CallSessionData):
        """Create persistent database record for session."""
        try:
            await asyncio.to_thread(
                lambda: CallSession.objects.create(
                    tenant_id=session_data.tenant_id,
                    session_id=session_data.session_id,
                    channel_id=session_data.channel_id,
                    caller_id=session_data.caller_id,
                    caller_name=session_data.caller_name or '',
                    dialed_number=session_data.dialed_number or '',
                    call_direction=session_data.call_direction,
                    status=session_data.status,
                    start_time=session_data.start_time,
                    context=session_data.context or ''
                )
            )
        except Exception as e:
            logger.error(f"Error creating database session: {e}")
    
    async def _update_database_session(self, session, updates: Dict[str, Any]):
        """Update persistent database record for session."""
        try:
            await asyncio.to_thread(
                lambda: CallSession.objects.filter(
                    session_id=session.session_id
                ).update(**{k: v for k, v in updates.items() 
                           if k in ['status', 'end_time', 'hangup_cause']})
            )
        except Exception as e:
            logger.error(f"Error updating database session: {e}")
    
    async def _update_database_session_by_id(self, session_id: str, updates: Dict[str, Any]):
        """Update persistent database record by session ID."""
        try:
            await asyncio.to_thread(
                lambda: CallSession.objects.filter(
                    session_id=session_id
                ).update(**{k: v for k, v in updates.items() 
                           if k in ['status', 'tenant_id', 'bridge_id']})
            )
        except Exception as e:
            logger.error(f"Error updating database session by ID: {e}")
    
    async def _get_recent_active_sessions(self, minutes: int = 5):
        """Get recently active sessions for bridge correlation."""
        try:
            from datetime import timedelta
            cutoff_time = timezone.now() - timedelta(minutes=minutes)
            
            # Get active sessions from Redis
            # This is a simplified approach - in production you'd want more
            # sophisticated session tracking
            return []  # Placeholder for now
            
        except Exception as e:
            logger.error(f"Error getting recent active sessions: {e}")
            return []
    
    async def _delayed_session_cleanup(self, session_id: str, delay: int = 300):
        """Clean up session after a delay."""
        try:
            await asyncio.sleep(delay)
            await self.session_manager.cleanup_session(session_id)
            logger.info(f"Cleaned up session {session_id} after {delay} seconds")
        except Exception as e:
            logger.error(f"Error in delayed cleanup for session {session_id}: {e}")
    
    def get_statistics(self) -> Dict[str, int]:
        """Get event processing statistics."""
        self._event_statistics['total'] = sum(
            count for event, count in self._event_statistics.items() 
            if event != 'total'
        )
        return self._event_statistics.copy()
    
    def reset_statistics(self):
        """Reset event processing statistics."""
        for key in self._event_statistics:
            self._event_statistics[key] = 0


# Global event handler instance
_event_handler: Optional[AMIEventHandler] = None


async def get_event_handler() -> AMIEventHandler:
    """Get or create global event handler instance."""
    global _event_handler
    if _event_handler is None:
        _event_handler = AMIEventHandler()
        await _event_handler.initialize()
    return _event_handler


async def setup_event_handlers_for_tenant(tenant_id: str):
    """Set up event handlers for a specific tenant."""
    handler = await get_event_handler()
    await handler.register_handlers(tenant_id)


async def cleanup_event_handler():
    """Cleanup global event handler instance."""
    global _event_handler
    _event_handler = None
