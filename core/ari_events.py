"""
ARI Event Handlers

This module provides comprehensive event handlers for ARI (Asterisk REST Interface) events
with focus on call state changes and integration with session management.

Following the same architectural patterns as ami_events.py for consistency.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime
from django.conf import settings
from django.utils import timezone

from .models import Tenant, CallSession
from .session_manager import get_session_manager, CallSessionData
from .ari_manager import get_ari_manager
from .call_routing import get_routing_engine, RoutingDecision

# Configure logging
logger = logging.getLogger(__name__)


@dataclass
class ARIEventStats:
    """Statistics for ARI event processing"""
    stasis_start_events: int = 0
    stasis_end_events: int = 0
    channel_state_change_events: int = 0
    bridge_created_events: int = 0
    bridge_destroyed_events: int = 0
    bridge_entered_events: int = 0
    bridge_left_events: int = 0
    recording_started_events: int = 0
    recording_finished_events: int = 0
    total_events_processed: int = 0
    total_errors: int = 0
    last_event_time: Optional[datetime] = None


class ARIEventHandler:
    """
    Comprehensive ARI event handler for call state management
    
    Handles various ARI events and integrates with session management
    for real-time call tracking and control.
    """
    
    def __init__(self):
        self.session_manager = get_session_manager()
        self.ari_manager = get_ari_manager()
        self.routing_engine = get_routing_engine()
        self.stats = ARIEventStats()
        self.custom_handlers: Dict[str, List[Callable]] = {}
        logger.info("ARI Event Handler initialized")

    async def handle_stasis_start(self, channel, event):
        """
        Handle StasisStart events (channel enters ARI application)
        
        This event is triggered when a channel enters the Stasis application,
        indicating that ARI now has control over the channel.
        """
        try:
            self.stats.stasis_start_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            channel_id = channel.id
            channel_name = channel.name
            caller_id = getattr(channel, 'caller', {}).get('number', 'Unknown')
            caller_name = getattr(channel, 'caller', {}).get('name', 'Unknown')
            
            logger.info(f"StasisStart: Channel {channel_id} ({channel_name}) entered application")
            logger.debug(f"Channel details: Caller ID: {caller_id}, Name: {caller_name}")
            
            # Extract tenant information from channel context or variables
            tenant_id = await self._extract_tenant_from_channel(channel)
            if not tenant_id:
                logger.warning(f"Could not determine tenant for channel {channel_id}")
                return
            
            # Get or create session for this channel
            session = await self.session_manager.get_session_by_channel(channel_id)
            if not session:
                # Create new session if not exists (fallback)
                session_data = CallSessionData(
                    session_id=channel_id,  # Use channel ID as fallback session ID
                    channel_id=channel_id,
                    tenant_id=tenant_id,
                    caller_id=caller_id,
                    caller_name=caller_name,
                    call_direction='unknown',  # Will be determined later
                    status='in_application',
                    created_at=timezone.now()
                )
                
                success = await self.session_manager.create_session(session_data)
                if not success:
                    logger.error(f"Failed to create session for channel {channel_id}")
                    return
                
                logger.info(f"Created new session for channel {channel_id} in application")
            else:
                # Update existing session status
                session.status = 'in_application'
                session.ari_control = True
                await self.session_manager.update_session(session.session_id, session)
                logger.info(f"Updated existing session {session.session_id} for channel {channel_id}")
            
            # Get routing decision based on tenant configuration
            routing_data = {
                'tenant_id': tenant_id,
                'caller_id': caller_id,
                'dialed_number': getattr(event, 'exten', None),
                'channel_id': channel_id,
                'direction': 'inbound',  # Assume inbound for StasisStart
                'timestamp': timezone.now()
            }
            
            routing_decision = await self.routing_engine.get_routing_decision(routing_data)
            logger.info(f"Routing decision for channel {channel_id}: {routing_decision.action}")
            
            # Execute routing action
            await self._execute_routing_action(channel, routing_decision, tenant_id, session)
            
            # Call custom handlers
            await self._call_custom_handlers('StasisStart', channel, event)
            
            # Log application entry for monitoring
            await self._log_application_event(tenant_id, 'stasis_start', {
                'channel_id': channel_id,
                'channel_name': channel_name,
                'caller_id': caller_id,
                'caller_name': caller_name,
                'routing_action': routing_decision.action,
                'routing_metadata': routing_decision.metadata
            })
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling StasisStart event for channel {getattr(channel, 'id', 'unknown')}: {e}")

    async def handle_stasis_end(self, channel, event):
        """
        Handle StasisEnd events (channel leaves ARI application)
        
        This event is triggered when a channel leaves the Stasis application,
        usually indicating the call has ended or been transferred.
        """
        try:
            self.stats.stasis_end_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            channel_id = channel.id
            channel_name = channel.name
            
            logger.info(f"StasisEnd: Channel {channel_id} ({channel_name}) left application")
            
            # Extract tenant information
            tenant_id = await self._extract_tenant_from_channel(channel)
            if not tenant_id:
                logger.warning(f"Could not determine tenant for channel {channel_id}")
                return
            
            # Update session status
            session = await self.session_manager.get_session_by_channel(channel_id)
            if session:
                session.status = 'completed'
                session.ari_control = False
                session.end_time = timezone.now()
                await self.session_manager.update_session(session.session_id, session)
                logger.info(f"Updated session {session.session_id} status to completed")
                
                # Clean up session after a delay
                asyncio.create_task(self._delayed_session_cleanup(session.session_id, 60))
            else:
                logger.warning(f"No session found for channel {channel_id} during StasisEnd")
            
            # Call custom handlers
            await self._call_custom_handlers('StasisEnd', channel, event)
            
            # Log application exit for monitoring
            await self._log_application_event(tenant_id, 'stasis_end', {
                'channel_id': channel_id,
                'channel_name': channel_name
            })
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling StasisEnd event for channel {getattr(channel, 'id', 'unknown')}: {e}")

    async def handle_channel_state_change(self, channel, event):
        """
        Handle ChannelStateChange events
        
        This event is triggered when a channel's state changes (Up, Down, Ringing, etc.)
        providing detailed call progress information.
        """
        try:
            self.stats.channel_state_change_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            channel_id = channel.id
            old_state = event.get('old_state', 'Unknown')
            new_state = channel.state
            
            logger.debug(f"ChannelStateChange: Channel {channel_id} changed from {old_state} to {new_state}")
            
            # Extract tenant information
            tenant_id = await self._extract_tenant_from_channel(channel)
            if not tenant_id:
                logger.debug(f"Could not determine tenant for channel {channel_id}")
                return
            
            # Update session with new state
            session = await self.session_manager.get_session_by_channel(channel_id)
            if session:
                # Map ARI states to our session states
                session_status = self._map_channel_state_to_status(new_state)
                if session_status != session.status:
                    old_status = session.status
                    session.status = session_status
                    await self.session_manager.update_session(session.session_id, session)
                    logger.info(f"Session {session.session_id} status changed from {old_status} to {session_status}")
            
            # Handle specific state transitions
            if new_state == 'Up':
                await self._handle_channel_answered(channel_id, tenant_id)
            elif new_state == 'Down':
                await self._handle_channel_hangup(channel_id, tenant_id)
            elif new_state == 'Ringing':
                await self._handle_channel_ringing(channel_id, tenant_id)
            
            # Call custom handlers
            await self._call_custom_handlers('ChannelStateChange', channel, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling ChannelStateChange event for channel {getattr(channel, 'id', 'unknown')}: {e}")

    async def handle_bridge_created(self, bridge, event):
        """Handle BridgeCreated events"""
        try:
            self.stats.bridge_created_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            bridge_id = bridge.id
            bridge_type = bridge.bridge_type
            
            logger.info(f"BridgeCreated: Bridge {bridge_id} of type {bridge_type} created")
            
            # Call custom handlers
            await self._call_custom_handlers('BridgeCreated', bridge, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling BridgeCreated event: {e}")

    async def handle_bridge_destroyed(self, bridge, event):
        """Handle BridgeDestroyed events"""
        try:
            self.stats.bridge_destroyed_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            bridge_id = bridge.id
            
            logger.info(f"BridgeDestroyed: Bridge {bridge_id} destroyed")
            
            # Call custom handlers
            await self._call_custom_handlers('BridgeDestroyed', bridge, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling BridgeDestroyed event: {e}")

    async def handle_channel_entered_bridge(self, channel, bridge, event):
        """Handle ChannelEnteredBridge events"""
        try:
            self.stats.bridge_entered_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            channel_id = channel.id
            bridge_id = bridge.id
            
            logger.info(f"ChannelEnteredBridge: Channel {channel_id} entered bridge {bridge_id}")
            
            # Update session with bridge information
            session = await self.session_manager.get_session_by_channel(channel_id)
            if session:
                if not session.metadata:
                    session.metadata = {}
                session.metadata['bridge_id'] = bridge_id
                session.metadata['bridge_type'] = bridge.bridge_type
                await self.session_manager.update_session(session.session_id, session)
                logger.debug(f"Updated session {session.session_id} with bridge information")
            
            # Call custom handlers
            await self._call_custom_handlers('ChannelEnteredBridge', channel, bridge, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling ChannelEnteredBridge event: {e}")

    async def handle_channel_left_bridge(self, channel, bridge, event):
        """Handle ChannelLeftBridge events"""
        try:
            self.stats.bridge_left_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            channel_id = channel.id
            bridge_id = bridge.id
            
            logger.info(f"ChannelLeftBridge: Channel {channel_id} left bridge {bridge_id}")
            
            # Update session metadata
            session = await self.session_manager.get_session_by_channel(channel_id)
            if session and session.metadata:
                session.metadata.pop('bridge_id', None)
                session.metadata.pop('bridge_type', None)
                await self.session_manager.update_session(session.session_id, session)
                logger.debug(f"Removed bridge information from session {session.session_id}")
            
            # Call custom handlers
            await self._call_custom_handlers('ChannelLeftBridge', channel, bridge, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling ChannelLeftBridge event: {e}")

    async def handle_recording_started(self, recording, event):
        """Handle RecordingStarted events"""
        try:
            self.stats.recording_started_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            recording_name = recording.name
            target_uri = recording.target_uri
            
            logger.info(f"RecordingStarted: Recording {recording_name} started for {target_uri}")
            
            # Call custom handlers
            await self._call_custom_handlers('RecordingStarted', recording, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling RecordingStarted event: {e}")

    async def handle_recording_finished(self, recording, event):
        """Handle RecordingFinished events"""
        try:
            self.stats.recording_finished_events += 1
            self.stats.total_events_processed += 1
            self.stats.last_event_time = timezone.now()
            
            recording_name = recording.name
            target_uri = recording.target_uri
            
            logger.info(f"RecordingFinished: Recording {recording_name} finished for {target_uri}")
            
            # Call custom handlers
            await self._call_custom_handlers('RecordingFinished', recording, event)
            
        except Exception as e:
            self.stats.total_errors += 1
            logger.error(f"Error handling RecordingFinished event: {e}")

    def register_custom_handler(self, event_type: str, handler: Callable):
        """Register a custom event handler"""
        if event_type not in self.custom_handlers:
            self.custom_handlers[event_type] = []
        self.custom_handlers[event_type].append(handler)
        logger.debug(f"Registered custom handler for {event_type}")

    def unregister_custom_handler(self, event_type: str, handler: Callable):
        """Unregister a custom event handler"""
        if event_type in self.custom_handlers:
            try:
                self.custom_handlers[event_type].remove(handler)
                logger.debug(f"Unregistered custom handler for {event_type}")
            except ValueError:
                logger.warning(f"Handler not found for {event_type}")

    def get_statistics(self) -> ARIEventStats:
        """Get event processing statistics"""
        return self.stats

    async def _extract_tenant_from_channel(self, channel) -> Optional[str]:
        """Extract tenant ID from channel context or variables"""
        try:
            # Try to get tenant from channel variables
            if hasattr(channel, 'channelvars'):
                tenant_var = channel.channelvars.get('TENANT_ID')
                if tenant_var:
                    return tenant_var
            
            # Try to extract from channel name/context
            channel_name = getattr(channel, 'name', '')
            if 'tenant_' in channel_name:
                # Extract tenant ID from channel name pattern
                parts = channel_name.split('tenant_')
                if len(parts) > 1:
                    tenant_part = parts[1].split('_')[0]
                    return tenant_part
            
            # Fallback: try to get from dialplan context
            context = getattr(channel, 'dialplan', {}).get('context', '')
            if 'tenant_' in context:
                parts = context.split('tenant_')
                if len(parts) > 1:
                    tenant_part = parts[1].split('_')[0]
                    return tenant_part
            
            return None
            
        except Exception as e:
            logger.error(f"Error extracting tenant from channel: {e}")
            return None

    def _map_channel_state_to_status(self, channel_state: str) -> str:
        """Map ARI channel states to session status"""
        state_mapping = {
            'Down': 'completed',
            'Rsrvd': 'reserved',
            'OffHook': 'offhook',
            'Dialing': 'dialing',
            'Ring': 'ringing',
            'Ringing': 'ringing',
            'Up': 'answered',
            'Busy': 'busy',
            'Dialing Offhook': 'dialing',
            'Pre-ring': 'pre_ring',
            'Unknown': 'unknown'
        }
        
        return state_mapping.get(channel_state, 'unknown')

    async def _handle_channel_answered(self, channel_id: str, tenant_id: str):
        """Handle channel answered state"""
        logger.info(f"Channel {channel_id} answered for tenant {tenant_id}")
        
        # Update session timing
        session = await self.session_manager.get_session_by_channel(channel_id)
        if session:
            session.answer_time = timezone.now()
            await self.session_manager.update_session(session.session_id, session)

    async def _handle_channel_hangup(self, channel_id: str, tenant_id: str):
        """Handle channel hangup state"""
        logger.info(f"Channel {channel_id} hung up for tenant {tenant_id}")
        
        # Update session end time
        session = await self.session_manager.get_session_by_channel(channel_id)
        if session:
            session.end_time = timezone.now()
            session.status = 'completed'
            await self.session_manager.update_session(session.session_id, session)

    async def _handle_channel_ringing(self, channel_id: str, tenant_id: str):
        """Handle channel ringing state"""
        logger.debug(f"Channel {channel_id} ringing for tenant {tenant_id}")

    async def _call_custom_handlers(self, event_type: str, *args):
        """Call all custom handlers for an event type"""
        if event_type in self.custom_handlers:
            for handler in self.custom_handlers[event_type]:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(*args)
                    else:
                        handler(*args)
                except Exception as e:
                    logger.error(f"Error in custom handler for {event_type}: {e}")

    async def _log_application_event(self, tenant_id: str, event_type: str, data: dict):
        """Log application events for monitoring and debugging"""
        try:
            # This could be enhanced to store events in database or send to monitoring system
            logger.debug(f"Application event for tenant {tenant_id}: {event_type} - {data}")
        except Exception as e:
            logger.error(f"Error logging application event: {e}")

    async def _execute_routing_action(self, channel, routing_decision: RoutingDecision, 
                                     tenant_id: str, session):
        """Execute the routing action based on routing decision"""
        try:
            action = routing_decision.action
            channel_id = channel.id
            
            # Get ARI connection for tenant
            ari_connection = await self.ari_manager.get_connection(tenant_id)
            if not ari_connection:
                logger.error(f"No ARI connection available for tenant {tenant_id}")
                return
            
            # Update session with routing metadata
            if session and routing_decision.metadata:
                session.session_metadata.update(routing_decision.metadata)
                await self.session_manager.update_session(session.session_id, session)
            
            if action == 'answer':
                # Answer the call and set up recording/bridging if configured
                success = await ari_connection.answer_call(channel_id)
                if success:
                    logger.info(f"Call answered for channel {channel_id}")
                    
                    # Start recording if enabled
                    if routing_decision.recording_enabled:
                        recording_name = f"recording_{channel_id}_{int(timezone.now().timestamp())}"
                        await ari_connection.start_recording(channel_id, recording_name)
                        logger.info(f"Recording started: {recording_name}")
                
            elif action == 'reject':
                # Reject/hangup the call
                await ari_connection.hangup_call(channel_id)
                logger.info(f"Call rejected for channel {channel_id}")
            
            else:
                logger.warning(f"Routing action '{action}' not fully implemented, defaulting to answer")
                await ari_connection.answer_call(channel_id)
                
        except Exception as e:
            logger.error(f"Error executing routing action {routing_decision.action}: {e}")
            # Fallback to basic answer on error
            try:
                ari_connection = await self.ari_manager.get_connection(tenant_id)
                if ari_connection:
                    await ari_connection.answer_call(channel.id)
            except:
                pass  # Best effort fallback

    async def _delayed_session_cleanup(self, session_id: str, delay_seconds: int):
        """Clean up session after a delay"""
        try:
            await asyncio.sleep(delay_seconds)
            await self.session_manager.end_session(session_id)
            logger.debug(f"Cleaned up session {session_id} after {delay_seconds} seconds")
        except Exception as e:
            logger.error(f"Error in delayed session cleanup for {session_id}: {e}")


# Global ARI event handler instance
_ari_event_handler = None

async def get_ari_event_handler() -> ARIEventHandler:
    """Get global ARI event handler instance (singleton pattern)"""
    global _ari_event_handler
    if _ari_event_handler is None:
        _ari_event_handler = ARIEventHandler()
    return _ari_event_handler

async def setup_ari_event_handlers_for_tenant(tenant_id: str):
    """Setup ARI event handlers for a specific tenant"""
    try:
        handler = await get_ari_event_handler()
        ari_manager = get_ari_manager()
        
        # Get ARI connection for tenant
        connection = await ari_manager.get_connection(tenant_id)
        if not connection:
            logger.error(f"No ARI connection available for tenant {tenant_id}")
            return False
        
        # Register event handlers with the ARI connection
        connection.register_event_handler('StasisStart', handler.handle_stasis_start)
        connection.register_event_handler('StasisEnd', handler.handle_stasis_end)
        connection.register_event_handler('ChannelStateChange', handler.handle_channel_state_change)
        connection.register_event_handler('BridgeCreated', handler.handle_bridge_created)
        connection.register_event_handler('BridgeDestroyed', handler.handle_bridge_destroyed)
        connection.register_event_handler('ChannelEnteredBridge', handler.handle_channel_entered_bridge)
        connection.register_event_handler('ChannelLeftBridge', handler.handle_channel_left_bridge)
        connection.register_event_handler('RecordingStarted', handler.handle_recording_started)
        connection.register_event_handler('RecordingFinished', handler.handle_recording_finished)
        
        logger.info(f"ARI event handlers setup for tenant {tenant_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error setting up ARI event handlers for tenant {tenant_id}: {e}")
        return False

async def cleanup_ari_event_handler():
    """Cleanup global ARI event handler"""
    global _ari_event_handler
    if _ari_event_handler:
        # Clear custom handlers
        _ari_event_handler.custom_handlers.clear()
        _ari_event_handler = None
        logger.info("ARI event handler cleaned up")
