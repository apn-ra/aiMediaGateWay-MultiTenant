# Author: RA
# Purpose: Asterisk Manager Events
# Created: 23/09/2025

import logging
import asyncio
from core.ami.manager import AMIConnection
from core.session.manager import get_session_manager
from core.ari.manager import ARIConnection
from core.ami.manager import get_ami_manager
from core.ari.events import ARIEventHandler
from typing import Dict, Any, Optional
from django.utils import timezone
from decouple import config

logger = logging.getLogger(__name__)


class AMIEventHandler:

    def __init__(self):
        self.ari_event_handler = None
        self.tenant_id = None
        self.session_manager = None
        self.ami_manager = None
        self._tenant_cache = {}
        self._event_statistics = {
            'new_channel': 0,
            'dial': 0,
            'hangup': 0,
            'bridge': 0,
            'varset': 0,
            'total': 0
        }

    async def initialize(self, tenant_id:int):
        """Initialize the event handler with managers."""
        self.session_manager = get_session_manager()
        self.ami_manager = await get_ami_manager()
        self.ari_event_handler = ARIEventHandler()
        self.tenant_id = tenant_id

    async def register_ari_handler(self, tenant_id:int) -> ARIConnection:
        """Register all ari event handlers for a specific tenant."""
        return await self.ari_event_handler.register_handlers(tenant_id=tenant_id)

    async def register_ami_handlers(self, tenant_id: int) -> AMIConnection:
        """Register all ami event handlers for a specific tenant."""
        await self.initialize(tenant_id=tenant_id)

        # Register event handlers with the AMI manager
        await self.ami_manager.register_event_handler(
            tenant_id, 'BridgeEnter', self.handle_bridge_enter
        )

        await self.ami_manager.register_event_handler(
            tenant_id, 'Hangup', self.handle_hangup
        )

        logger.info(f"Registered AMI event handlers for tenant {tenant_id}")
        return self.ami_manager.connections[tenant_id]

    async def handle_bridge_enter(self, manager, message: Dict[str, Any]):
        """Handle Bridge enter events."""
        session = await self.session_manager.create_session_from_ami_bridge_enter_event(self.tenant_id, message)
        session.rtp_endpoint_host = config('AI_MEDIA_GATEWAY_HOST')

        session.asterisk_host = self.ami_manager.connections[self.tenant_id].config.host
        sessionId = await self.session_manager.create_session(session_data=session)

        client = self.ari_event_handler.connections[self.tenant_id].client
        await client.snoop_channel(
            channel_id=sessionId,
            spy='both'
        )

    async def handle_hangup(self, manager, message: Dict[str, Any]):
        """
        Handle Hangup events for session cleanup.

        This cleans up sessions when calls end.
        """
        uniqueid = message.get('Uniqueid', '')

        # Get existing session
        session = await self.session_manager.get_session(uniqueid)
        if session:
            updates = {
                'status': 'completed',
                'call_end_time': timezone.now(),
                'metadata': {
                    'hangup_code': message.get('Cause'),
                    'hangup_cause': message.get('Cause-txt', '')
                }
            }
            await self.session_manager.update_session(uniqueid, updates)

            # Schedule session cleanup after a delay
            asyncio.create_task(self._delayed_session_cleanup(uniqueid, delay=300))  # 5 minutes
        else:
            logger.warning(f"No session found for hangup event uniqueid: {uniqueid}")

    async def _delayed_session_cleanup(self, session_id: str, delay: int = 300):
        """Clean up session after a delay."""
        try:
            await asyncio.sleep(delay)
            await self.session_manager.cleanup_session(session_id)
            logger.info(f"Cleaned up session {session_id} after {delay} seconds")
        except Exception as e:
            logger.error(f"Error in delayed cleanup for session {session_id}: {e}")


    async def handle_new_channel(self, manager, message: Dict[str, Any]):
        """
        Handle New channel events for early call detection.

        This is triggered when a new channel is created in Asterisk,
        allowing us to create sessions before the call is answered.
        """

        logger.info("New channel event")
