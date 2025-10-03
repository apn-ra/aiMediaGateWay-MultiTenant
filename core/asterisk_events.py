# Author: RA
# Purpose: Asterisk Events
# Created: 03/10/2025
import asyncio
import logging

from typing import Dict, Any
from django.utils import timezone
from core.ami.manager import get_ami_manager
from core.ari.manager import get_ari_manager
from core.junie_codes.rtp_integration import get_rtp_integrator
from core.session.manager import get_session_manager
from decouple import config

logger = logging.getLogger(__name__)

class AsteriskEventHandlers:
    """
    AsteriskEventHandlers class manages various Asterisk-related event handlers for a specific tenant.

    This class is responsible for handling communication and events related to AMI (Asterisk
    Manager Interface) and ARI (Asterisk REST Interface) for a specific tenant in a multi-tenant
    environment. It initializes managers, registers event handlers, manages session lifecycles,
    and performs session cleanup tasks.

    :ivar bridges: A dictionary to track the active bridges for each tenant.
    :type bridges: dict
    :ivar tenant_id: The ID of the tenant for which the event handlers are configured.
    :type tenant_id: int
    :ivar session_manager: Responsible for managing sessions and interactions with sessions.
    :type session_manager: Optional[Any]
    :ivar ami_manager: Manager for handling AMI (Asterisk Manager Interface) connections and events.
    :type ami_manager: Optional[Any]
    :ivar ari_manager: Manager for handling ARI (Asterisk REST Interface) connections and events.
    :type ari_manager: Optional[Any]
    """
    def __init__(self, tenant_id:int):
        self.rtp_integrator = None
        self.bridges = {}
        self.tenant_id = tenant_id
        self._ari_task = None
        self._ami_task = None

        self.session_manager = None
        self.ami_manager = None
        self.ari_manager = None

    async def initialize(self):
        """Initialize the event handler with managers."""
        self.rtp_integrator = get_rtp_integrator()
        self.session_manager = get_session_manager()
        self.ami_manager = await get_ami_manager()
        self.ari_manager = get_ari_manager()

        await asyncio.sleep(0.01)
        await self.register_ari_handlers()
        await self.register_ami_handlers()

        self._ami_task = asyncio.create_task(self.ami_manager.connections[self.tenant_id].connect(), name="ami-connection")
        self._ari_task = asyncio.create_task(self.ari_manager.connections[self.tenant_id].connect_websocket(), name="ari-connection")

    async def disconnect(self):
        """Disconnect all event handlers."""
        if self._ami_task and not self._ami_task.done():
            self._ami_task.cancel()
        if self._ari_task and not self._ari_task.done():
            self._ari_task.cancel()

        try:
            await self._ami_task
            await self._ari_task
        except asyncio.CancelledError:
            logger.info("AMI & ARI connection task cancelled.")

        await self.ari_manager.disconnect_all()
        await self.ami_manager.disconnect_all()
        logger.info("Event handlers disconnected")

    async def register_ari_handlers(self):
        """Register all ari event handlers for a specific tenant."""
        await self.ari_manager.register_event_handler(
            self.tenant_id, 'StasisStart', self.handle_StasisStart
        )

        await self.ari_manager.register_event_handler(
            self.tenant_id, 'StasisEnd', self.handle_StasisEnd
        )

    async def register_ami_handlers(self):
        """Register all ami event handlers for a specific tenant."""

        # Register event handlers with the AMI manager
        await self.ami_manager.register_event_handler(
            self.tenant_id, 'BridgeEnter', self.handle_ami_bridge_enter
        )

        await self.ami_manager.register_event_handler(
            self.tenant_id, 'Hangup', self.handle_ami_hangup
        )

        if self.tenant_id not in self.bridges:
            self.bridges[self.tenant_id] = []

    async def handle_StasisStart(self, event_data):
        """Handle StasisStart events."""
        channel = event_data['channel']
        channel_id = channel['id']

        if len(event_data['args']) == 2 and event_data['args'][0] == 'session_id':
           success = await self.rtp_integrator.integrate_session(session_id=event_data['args'][1])
           if success:
               # Get updated session data
               session = await self.session_manager.get_session(event_data['args'][1])

               client = self.ari_manager.connections[self.tenant_id].client
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
               await self.session_manager.update_session(session.session_id, {
                       'snoop_channel_id': ' ',
                       'external_media_channel_id': em_channel.id
               })

    async def handle_StasisEnd(self, event_data):
        """Handle StasisEnd events."""
        channel = event_data['channel']
        channel_id = channel['id']

    async def handle_ami_bridge_enter(self, manager, message: Dict[str, Any]):
        """Handle Bridge enter events."""
        if self.session_manager.is_inbound_channel(message.get('channel')):
            session_data = await self.session_manager.convert_to_call_session_data(
                tenant_id=self.tenant_id,
                message=message
            )

            session_data.rtp_endpoint_host = config('AI_MEDIA_GATEWAY_HOST')
            session_data.asterisk_host = self.ami_manager.connections[self.tenant_id].config.host

            if len(self.bridges[self.tenant_id]) == 0:
                self.bridges[self.tenant_id].append(session_data.bridge_id)

                session_id = await self.session_manager.create_session(session_data=session_data)
                client = self.ari_manager.connections[self.tenant_id].client
                await client.snoop_channel(
                    channel_id=session_data.channel.id,
                    spy='both',
                    appArgs=f"session_id,{session_id}"
                )

    async def handle_ami_hangup(self, manager, message: Dict[str, Any]):
        """
        Handle Hangup events for session cleanup.

        This cleans up sessions when calls end.
        """
        uniqueid = message.get('Uniqueid', '')

        if self.session_manager.is_inbound_channel(message.get('channel')):
            # Get an existing session
            session = await self.session_manager.get_session(uniqueid)
            if session and session.bridge_id in self.bridges[self.tenant_id]:
                await self.rtp_integrator.de_integrate_session(session_id=session.session_id)

                try:
                    await self.ari_manager.connections[self.tenant_id].client.hangup_channel(
                        channel_id=session.external_media_channel_id,
                        reason='normal'
                    )
                except Exception as e:
                    logger.error(f"Error in Hangup on externalMedia : {e}")

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
        """Cleanup session after a delay."""
        try:
            await asyncio.sleep(delay)
            await self.session_manager.cleanup_session(session_id)
            logger.info(f"Cleaned up session {session_id} after {delay} seconds")
        except Exception as e:
            logger.error(f"Error in delayed cleanup for session {session_id}: {e}")
