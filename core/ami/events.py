# Author: RA
# Purpose: Asterisk Manager Events
# Created: 23/09/2025
import asyncio
import logging
from typing import Dict, Any

from django.utils import timezone

from core.session.manager import CallSessionData
from decouple import config

logger = logging.getLogger(__name__)

class AMIEventOrganizer:
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

    async def handle_varset(self, manager, message: Dict[str, Any]):
        """Handle Varset events."""
        self._event_statistics['varset'] += 1

    async def handle_new_channel(self, manager, message: Dict[str, Any]):
        """Handle Newchannel events."""
        self._event_statistics['new_channel'] += 1
        if self.main.session_manager.is_inbound_channel(message.get('channel')):
            session_data = await self._create_call_session(message)
            if session_data.channel.dialplan.exten != 's':
                await self.main.session_manager.get_or_create__session(session_data=session_data)

    async def handle_bridge_enter(self, manager, message: Dict[str, Any]):
        """Handle Bridge enter events."""
        self._event_statistics['bridge'] += 1
        if self.main.session_manager.is_inbound_channel(message.get('channel')):
            session_data = await self._create_call_session(message)

            session_id = await self.main.session_manager.get_or_create__session(session_data=session_data)
            client = self.main.ari_manager.connections[self.main.tenant_id].client

            if session_data.channel.dialplan.exten != '':
                await client.snoop_channel(
                    channel_id=session_data.channel.id,
                    spy='both',
                    appArgs=f"session_id,{session_id}"
                )

    async def handle_hangup(self, manager, message: Dict[str, Any]):
        """
                Handle Hangup events for session cleanup.

                This cleans up sessions when calls end.
                """
        uniqueid = message.get('Uniqueid', '')
        self._event_statistics['hangup'] += 1

        if self.main.session_manager.is_inbound_channel(message.get('channel')):
            # Get an existing session
            session = await self.main.session_manager.get_session(uniqueid)
            if session:
                if session.channel.dialplan.exten != '' and session.external_media_channel_id:
                    await self.main.rtp_integrator.de_integrate_session(session_id=session.session_id)
                    try:
                        await self.main.ari_manager.connections[self.main.tenant_id].client.hangup_channel(
                            channel_id=session.external_media_channel_id,
                            reason='normal'
                        )
                    except Exception as e:
                        logger.error(f"Error in Hangup on externalMedia : {e}")

                metadata = session.metadata
                metadata['hangup_code'] = message.get('Cause')
                metadata['hangup_cause'] = message.get('Cause-txt', '')

                if session.call_answer_time:
                    session.call_end_time = timezone.now()

                updates = {
                    'status': 'completed',
                    'call_end_time': session.call_end_time,
                    'metadata': metadata,
                }

                await self.main.session_manager.update_session(uniqueid, updates)

                # Schedule session cleanup after a delay
                asyncio.create_task(self._delayed_session_cleanup(uniqueid, delay=300))  # 5 minutes
            else:
                logger.warning(f"No session found for hangup event uniqueid: {uniqueid}")
    async def _create_call_session(self, message: Dict[str, Any]) -> CallSessionData:
        """Create call session."""
        session_data = await self.main.session_manager.convert_to_call_session_data(
            tenant_id=self.main.tenant_id,
            message=message
        )

        session_data.rtp_endpoint_host = config('AI_MEDIA_GATEWAY_HOST')
        session_data.asterisk_host = self.main.ami_manager.connections[self.main.tenant_id].config.host
        return session_data

    async def _delayed_session_cleanup(self, session_id: str, delay: int = 300):
        """Cleanup session after a delay."""
        try:
            await asyncio.sleep(delay)
            await self.main.session_manager.cleanup_session(session_id)
            logger.info(f"Cleaned up session {session_id} after {delay} seconds")
        except Exception as e:
            logger.error(f"Error in delayed cleanup for session {session_id}: {e}")
