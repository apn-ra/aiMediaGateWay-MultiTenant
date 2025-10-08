# Author: RA
# Purpose: 
# Created: 24/09/2025
import logging
import asyncio

from django.utils import timezone

logger = logging.getLogger(__name__)

class ARIEventOrganizer:
    def __init__(self, main: "AsteriskEventHandlers"):
        self.main = main
        self._event_statistics = {
            'new_channel': 0,
            'dial': 0,
            'hangup': 0,
            'bridge': 0,
            'varset': 0,
            'total': 0
        }

    async def handle_stasis_start(self, event_data):
        """Handle StasisStart events."""
        channel = event_data['channel']
        channel_id = channel['id']

        if len(event_data['args']) == 2 and event_data['args'][0] == 'session_id':
           success = await self.main.rtp_integrator.integrate_session(session_id=event_data['args'][1])
           if success:
               # Get updated session data
               session = await self.main.session_manager.get_session(event_data['args'][1])

               client = self.main.ari_manager.connections[self.main.tenant_id].client
               externalHost = f"{session.rtp_endpoint_host}:{session.rtp_endpoint_port}"

               # Create external media channel and bridge
               em_channel = await client.create_external_media(
                   external_host=externalHost,
                   codec='ulaw'
               )

               bridge = await client.create_bridge(bridge_type='mixing')
               await bridge.add_channel(em_channel.id)
               await bridge.add_channel(channel_id)

               # Update session data
               await self.main.session_manager.update_session(session.session_id, {
                       'call_answer_time': timezone.now(),
                       'status': 'bridged',
                       'snoop_channel_id': channel_id,
                       'external_media_channel_id': em_channel.id
               })

    async def handle_stasis_end(self, event_data):
        """Handle StasisEnd events."""
        channel = event_data['channel']
        channel_id = channel['id']
