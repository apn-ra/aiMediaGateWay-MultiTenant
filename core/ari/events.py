# Author: RA
# Purpose: 
# Created: 24/09/2025
import logging
import asyncio

from numpy.random.tests.test_generator_mt19937 import endpoint

logger = logging.getLogger(__name__)

from decouple import config
from core.session.manager import get_session_manager
from core.ari.manager import get_ari_manager, ARIConnection
from core.junie_codes.rtp_integration import get_rtp_integrator
from django.utils import timezone

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
        self.rtp_integrator = get_rtp_integrator()
        self.tenant_id = tenant_id

        await self.rtp_integrator.start()

    async def register_handlers(self, tenant_id: int) -> ARIConnection:
        """Register all event handlers for a specific tenant."""
        await self.initialize(tenant_id)

        await self.ari_manager.register_event_handler(
            tenant_id, 'StasisStart', self.handle_StasisStart
        )

        await self.ari_manager.register_event_handler(
            tenant_id, 'StasisEnd', self.handle_StasisEnd
        )

        await self.ari_manager.register_event_handler(
            tenant_id, 'Dial', self.handle_dial
        )

        # await self.ari_manager.register_event_handler(
        #     tenant_id, 'ChannelDestroyed', self.handle_channel_state_change
        # )

        # await self.ari_manager.register_event_handler(
        #     tenant_id, 'ChannelStateChange', self.handle_channel_state_change
        # )

        return self.ari_manager.connections[tenant_id]

    async def handle_dial(self, event_data):
        """Handle dial events."""
        self._event_statistics['dial'] += 1
        logger.info(f"Dialing Event Data: {event_data}")
        if event_data['dialstatus'] == 'ANSWER':
            logger.info(f"Adding Channel {event_data['peer']['id']} to bridge {self.bridges[self.tenant_id].id}")
            await self.bridges[self.tenant_id].add_channel(event_data['peer']['id'])

    async def handle_channel_state_change(self, event_data):
        """Handle channel state change events."""
        channel = event_data['channel']
        channel_id = channel['id']
        channel_state = channel['state']

    async def handle_StasisStart(self, event_data):
        """Handle StasisStart events."""
        channel = event_data['channel']
        channel_id = channel['id']

        client = self.ari_manager.connections[self.tenant_id].client
        event_data['asterisk_host'] = self.ari_manager.connections[self.tenant_id].config.host
        event_data['rtp_endpoint_host'] = config('AI_MEDIA_GATEWAY_HOST')

        try:
            # Create a new session
            session_data = self.session_manager.create_session_from_event(self.tenant_id, event_data)
            if session_data:
                session_id = await self.session_manager.create_session(session_data)
                exten = session_data.channel.dialplan.exten
                caller_id = session_data.channel.caller.name or ''

                if session_id:
                    logger.info(f"Created session { session_id } for channelId { channel_id }")
                    self._event_statistics['new_channel'] += 1

                else:
                    logger.error(f"Failed to create session for channel { channel }")

                # Provide slight delay
                await asyncio.sleep(0.1)

                # Integrate the session
                # status = await self.rtp_integrator.integrate_session(session_id=session_id)
                # if status:
                self.bridges[self.tenant_id] = await client.create_bridge(bridge_type="mixing")
                await self.bridges[self.tenant_id].add_channel(channel_id)

                await client.create_channel(endpoint=f"PJSIP/{exten}", app="live-transcript", app_args="--no-video")

                    # session = await self.session_manager.get_session(session_id)
                    # logger.info(f"ExternalMedia Host: {session.rtp_endpoint_host} Port: {session.rtp_endpoint_port}")
                    # external = await client.create_external_media(
                    #     app=client.config.app_name,
                    #     external_host=f"{session.rtp_endpoint_host}:{session.rtp_endpoint_port}",
                    #     codec="slin16",
                    #     transport="rtp",
                    #     direction="in",
                    #     connection_type="client",
                    # )
                    #
                    # await bridge.add_channel(channel_id)
                    # await bridge.add_channel(external.id)
                    # self._event_statistics['bridge'] += 1
                    # logger.info(f"ExternalMedia connected: {external.id}")
            else:
                logger.error(f"Failed to create session for event data { event_data }")

        except Exception as e:
            logger.error(f"Error handling call: {e}")

    async def handle_StasisEnd(self, event_data):
        """Handle StasisEnd events."""
        channel = event_data['channel']
        sessionId = channel['protocol_id']
        channel_id = channel['id']

        # Get existing session
        session = await self.session_manager.get_session(sessionId)
        if session:
            # Close RTP connection
            await self.rtp_integrator.de_integrate_session(session_id=session.session_id)

            # Update session with hangup information
            end_time = timezone.now()
            duration = None
            if session.call_start_time:
                duration = (end_time - session.call_start_time).total_seconds()
                updates = {
                    'status': 'completed',
                    'call_end_time': end_time,
                    'duration': duration
                }
            await self.session_manager.update_session(sessionId, updates)
            # Schedule session cleanup after a delay
            asyncio.create_task(self._delayed_session_cleanup(sessionId, delay=300))  # 5 minutes
        else:
            logger.warning(f"No session found for hangup event: {channel}")

        self._event_statistics['hangup'] += 1

    async def _delayed_session_cleanup(self, session_id: str, delay: int = 300):
        """Clean up session after a delay."""
        try:
            await asyncio.sleep(delay)
            await self.session_manager.cleanup_session(session_id)
            logger.info(f"Cleaned up session {session_id} after {delay} seconds")
        except Exception as e:
            logger.error(f"Error in delayed cleanup for session {session_id}: {e}")
