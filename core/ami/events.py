# Author: RA
# Purpose: Asterisk Manager Events
# Created: 23/09/2025

import logging
import asyncio
from core.ami.manager import AMIConnection
from core.session.manager import SessionManager
from core.ari.manager import ARIConnection
from core.ami.manager import get_ami_manager
from core.ari.events import ARIEventHandler
from typing import Dict, Any
from django.utils import timezone
from decouple import config

logger = logging.getLogger(__name__)


class AMIEventHandler:

    def __init__(self, tenant_id:int, session_manager:SessionManager = None):
        self.bridges = {}
        self._ami_task = None
        self.tenant_id = tenant_id
        self.session_manager = session_manager
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

    async def initialize(self):
        """Initialize the event handler with managers."""
        self.ami_manager = await get_ami_manager()

        await asyncio.sleep(0.01)
        ami_connection = await self.register_ami_handlers()

        self._ami_task = asyncio.create_task(ami_connection.connect(), name="ami-connection")

    async def disconnect(self):
        """Disconnect all event handlers."""

        if self._ami_task and not self._ami_task.done():
            self._ami_task.cancel()

        try:
            await self._ami_task
        except asyncio.CancelledError:
            logger.info("AMI & ARI connection task cancelled.")

        await self.ami_manager.disconnect_all()
        logger.info("Event handlers disconnected")

    async def register_ami_handlers(self) -> AMIConnection:
        """Register all ami event handlers for a specific tenant."""

        # Register event handlers with the AMI manager
        await self.ami_manager.register_event_handler(
            self.tenant_id, 'BridgeEnter', self.handle_bridge_enter
        )

        await self.ami_manager.register_event_handler(
            self.tenant_id, 'Hangup', self.handle_hangup
        )

        if self.tenant_id not in self.bridges:
            self.bridges[self.tenant_id] = []

        logger.info(f"Registered AMI event handlers for tenant {self.tenant_id}")
        return self.ami_manager.connections[self.tenant_id]

    async def handle_bridge_enter(self, manager, message: Dict[str, Any]):
        """Handle Bridge enter events."""

        if self.session_manager.is_inbound_channel(message.get('channel')):
            session_data = await self.session_manager.convert_to_call_session_data(self.tenant_id, message)

            session_data.rtp_endpoint_host = config('AI_MEDIA_GATEWAY_HOST')
            session_data.asterisk_host = self.ami_manager.connections[self.tenant_id].config.host

            if len(self.bridges[self.tenant_id]) == 0:
                self.bridges[self.tenant_id].append(session_data.bridge_id)

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
            if session and session.bridge_id in self.bridges[self.tenant_id]:

                metadata = session.metadata
                metadata['hangup_code'] = message.get('Cause')
                metadata['hangup_cause'] = message.get('Cause-txt', '')

                updates = {
                    'status': 'completed',
                    'call_end_time': timezone.now(),
                    'metadata': metadata,
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
