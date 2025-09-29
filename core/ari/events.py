# Author: RA
# Purpose: 
# Created: 24/09/2025
import logging
import asyncio

logger = logging.getLogger(__name__)

from core.session.manager import get_session_manager
from core.ari.manager import get_ari_manager, ARIConnection

class ARIEventHandler:

    def __init__(self):
        self.rtp_integrator = None
        self.tenant_id = None
        self.session_manager = None
        self.ari_manager = None
        self._tenant_cache = {}
        self.bridges = {}
        self._event_statistics = {
            'new_channel': 0,
            'dial': 0,
            'hangup': 0,
            'bridge': 0,
            'varset': 0,
            'total': 0
        }

    async def initialize(self, tenant_id: int = None):
        """Initialize the event handler with managers."""
        self.session_manager = get_session_manager()
        self.ari_manager = get_ari_manager()
        self.tenant_id = tenant_id

    async def register_handlers(self, tenant_id: int) -> ARIConnection:
        """Register all event handlers for a specific tenant."""
        await self.initialize(tenant_id)

        await self.ari_manager.register_event_handler(
            tenant_id, 'StasisStart', self.handle_StasisStart
        )

        await self.ari_manager.register_event_handler(
            tenant_id, 'StasisEnd', self.handle_StasisEnd
        )

        return self.ari_manager.connections[tenant_id]

    async def handle_dial(self, event_data):
        """Handle dial events."""
        self._event_statistics['dial'] += 1
        logger.info(f"Dialing Event Data: {event_data}")

    async def handle_channel_state_change(self, event_data):
        """Handle channel state change events."""
        channel = event_data['channel']
        channel_id = channel['id']
        channel_state = channel['state']

    async def handle_StasisStart(self, event_data):
        """Handle StasisStart events."""
        channel = event_data['channel']
        channel_id = channel['id']
        # logger.info(f"Received StasisStart {event_data}")

    async def handle_StasisEnd(self, event_data):
        """Handle StasisEnd events."""
        channel = event_data['channel']
        channel_id = channel['id']

    async def _delayed_session_cleanup(self, session_id: str, delay: int = 300):
        """Clean up session after a delay."""
        try:
            await asyncio.sleep(delay)
            await self.session_manager.cleanup_session(session_id)
            logger.info(f"Cleaned up session {session_id} after {delay} seconds")
        except Exception as e:
            logger.error(f"Error in delayed cleanup for session {session_id}: {e}")
