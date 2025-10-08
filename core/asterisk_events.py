# Author: RA
# Purpose: Asterisk Events
# Created: 03/10/2025
import asyncio
import logging

from core.ami.events import AMIEventOrganizer
from core.ari.events import ARIEventOrganizer

from core.ami.manager import get_ami_manager
from core.ari.manager import get_ari_manager

from core.rtp_integration import get_rtp_integrator
from core.session.manager import get_session_manager

logger = logging.getLogger(__name__)

class AsteriskEventHandlers:
    """
    AsteriskEventHandlers class manages various Asterisk-related event handlers for a specific tenant.

    This class is responsible for handling communication and events related to AMI (Asterisk
    Manager Interface) and ARI (Asterisk REST Interface) for a specific tenant in a multi-tenant
    environment. It initializes managers, registers event handlers, manages session lifecycles,
    and performs session cleanup tasks.

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
        self.ari_events = None
        self.ami_events = None
        self.rtp_integrator = None
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

        self.ami_events = AMIEventOrganizer(self)
        self.ari_events = ARIEventOrganizer(self)

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
            self.tenant_id, 'StasisStart', self.ari_events.handle_stasis_start
        )

        await self.ari_manager.register_event_handler(
            self.tenant_id, 'StasisEnd', self.ari_events.handle_stasis_end
        )

    async def register_ami_handlers(self):
        """Register all ami event handlers for a specific tenant."""

        # Register event handlers with the AMI manager
        await self.ami_manager.register_event_handler(
            self.tenant_id, 'Newchannel', self.ami_events.handle_new_channel
        )

        await self.ami_manager.register_event_handler(
            self.tenant_id, 'BridgeEnter', self.ami_events.handle_bridge_enter
        )

        await self.ami_manager.register_event_handler(
            self.tenant_id, 'Hangup', self.ami_events.handle_hangup
        )
