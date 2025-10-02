# Author: RA
# Purpose: Asterisk Manager Events
# Created: 23/09/2025

import logging
import asyncio
from core.ami.manager import AMIConnection
from core.session.manager import get_session_manager
from core.junie_codes.rtp_integration import get_rtp_integrator
from core.ari.manager import ARIConnection
from core.ami.manager import get_ami_manager
from core.ari.events import ARIEventHandler
from typing import Dict, Any, Optional
from django.utils import timezone
from decouple import config

logger = logging.getLogger(__name__)


class AMIEventHandler:

    def __init__(self):
        self.bridge_id = None
        self.rtp_integrator = None
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
        self.rtp_integrator = get_rtp_integrator()
        self.session_manager = get_session_manager()
        self.ami_manager = await get_ami_manager()
        self.ari_event_handler = ARIEventHandler()
        self.tenant_id = tenant_id

        await self.rtp_integrator.start()

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

        if self.session_manager.is_inbound_channel(message.get('channel')):
            session_data = await self.session_manager.create_session_from_ami_bridge_enter_event(self.tenant_id, message)

            session_data.rtp_endpoint_host = config('AI_MEDIA_GATEWAY_HOST')
            session_data.asterisk_host = self.ami_manager.connections[self.tenant_id].config.host

            if self.bridge_id is None:
                logger.info(f"Bridge Enter Event: {session_data}")
                self.bridge_id = session_data.bridge_id

                session_id = await self.session_manager.create_session(session_data=session_data)

                client = self.ari_event_handler.ari_manager.connections[self.tenant_id].client
                await client.snoop_channel(
                    channel_id=session_data.channel.id,
                    spy='both',
                    appArgs=f"session_id,{session_id}"
                )

                # await client.create_channel(
                #     endpoint='PJSIP/6002',
                #     app_args=f"snoop_channel,{snoop_channel.id}"
                # )

                # success = await self.rtp_integrator.integrate_session(session_id=session_data.session_id)
                # if success:
                #     session = await self.session_manager.get_session(session_data.session_id)
                #     client = self.ari_event_handler.ari_manager.connections[self.tenant_id].client
                #     externalHost = f"{session.rtp_endpoint_host}:{session.rtp_endpoint_port}"
                #     em_channel = await client.create_external_media(external_host=externalHost)
                #     bridge = await client.get_bridge(bridge_id=session.bridge_id)
                #     await bridge.add_channel(em_channel.id)
                #
                #     await self.session_manager.update_session(session.session_id, {
                #             'snoop_channel_id': ' ',
                #             'external_media_channel_id': em_channel.id
                #     })
            # else:
            #     logger.info(f"Session already exist:  {session_data}")

    async def handle_hangup(self, manager, message: Dict[str, Any]):
        """
        Handle Hangup events for session cleanup.

        This cleans up sessions when calls end.
        """
        uniqueid = message.get('Uniqueid', '')

        if self.session_manager.is_inbound_channel(message.get('channel')):
            # Get an existing session
            session = await self.session_manager.get_session(uniqueid)
            if session and self.bridge_id == session.bridge_id:

                # Hangup ExternalMedia Channel
                client = self.ari_event_handler.ari_manager.connections[self.tenant_id].client
                # await client.hangup_channel(channel_id=session.external_media_channel_id, reason='normal')

                metadata = session.metadata
                metadata['hangup_code'] = message.get('Cause')
                metadata['hangup_cause'] = message.get('Cause-txt', '')

                updates = {
                    'status': 'completed',
                    'call_end_time': timezone.now(),
                    'metadata': metadata,
                }

                await self.session_manager.update_session(uniqueid, updates)

                await self.rtp_integrator.de_integrate_session(session_id=uniqueid)

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
