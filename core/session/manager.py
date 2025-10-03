# Author: RA
# Purpose: 
# Created: 24/09/2025

import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from dacite import from_dict, Config

from pydantic import UUID4

from core.models import CallSession
from django.conf import settings
from django.utils import timezone
import redis

logger = logging.getLogger(__name__)

@dataclass
class DialplanData:
    """Dialplan data structure"""
    context: str
    exten: str
    priority: str
    app_name: Optional[str] = None
    app_data: Optional[str] = None

@dataclass
class CallerData:
    """Caller data structure"""
    name: str
    number: str

@dataclass
class ConnectedData:
    """Connected data structure"""
    name: Optional[str] = None
    number: Optional[str] = None

@dataclass
class ChannelData:
    """Channel data structure"""
    id: str
    name: str
    state: str
    protocol_id: str
    caller: CallerData
    accountcode: str
    connected: ConnectedData
    dialplan: DialplanData
    creationtime: Optional[datetime] = None
    language: Optional[str] = 'en'

@dataclass
class CallSessionData:
    """Data class for call session information"""
    session_id: str
    tenant_id: int
    asterisk_id: str
    asterisk_host: str
    channel: ChannelData
    ari_control: bool = False
    direction: str = 'inbound'
    call_type: str = 'caller'
    status: str = 'detected'
    duration: Optional[int] = 0
    bridge_id: Optional[str] = None
    snoop_channel_id: Optional[str] = None
    external_media_channel_id: Optional[str] = None
    rtp_endpoint_host: Optional[str] = None
    rtp_endpoint_port: Optional[int] = None
    metadata: Dict[str, Any] = None
    call_start_time: Optional[datetime] = None
    call_answer_time: Optional[datetime] = None
    call_end_time: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.created_at is None:
            self.created_at = timezone.now()
        if self.updated_at is None:
            self.updated_at = timezone.now()

    def to_dict(self):
        return asdict(self)


class SessionManager:
    """
    Redis-backed session manager implementing session-first approach
    for zero-loss audio capture and multi-tenant isolation.
    """

    def __init__(self, redis_host='127.0.0.1', redis_port=6379, redis_db=0):
        """Initialize SessionManager with Redis connection"""
        try:
            self.redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # Test connection
            self.redis_client.ping()
            logger.info(f"SessionManager connected to Redis at {redis_host}:{redis_port}")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

        # Redis key prefixes for multi-tenant isolation
        self.SESSION_PREFIX = "session"
        self.TENANT_PREFIX = "tenant"
        self.CHANNEL_PREFIX = "channel"
        self.SESSION_EXPIRY = 86400  # 24 hours in seconds

    def _get_session_key(self, session_id: str) -> str:
        """Generate Redis key for session data"""
        return f"{self.SESSION_PREFIX}:{session_id}"

    def _get_tenant_session_key(self, tenant_id: int) -> str:
        """Generate Redis key for tenant session index"""
        return f"{self.TENANT_PREFIX}:{tenant_id}:sessions"

    def _get_channel_session_key(self, channel_id: str) -> str:
        """Generate Redis key for channel to session mapping"""
        return f"{self.CHANNEL_PREFIX}:{channel_id}"

    @staticmethod
    def _get_call_type(exten:str) -> str:
        if exten == '':
            return 'callee'
        return 'caller'

    async def convert_to_call_session_data(self,tenant_id:int, message: Dict[str, Any]) -> CallSessionData:
        unique = message.get('Uniqueid', str(uuid.uuid4()))
        asterisk_host = message.get('asterisk_host', 'localhost')
        rtp_endpoint_host = message.get('rtp_endpoint_host', 'localhost')
        call_type = self._get_call_type(message.get('Exten', ''))

        return CallSessionData(
            session_id=unique,
            tenant_id=tenant_id,
            asterisk_id=unique,
            asterisk_host=asterisk_host,
            rtp_endpoint_host=rtp_endpoint_host,
            bridge_id=message.get('BridgeUniqueid'),
            direction='inbound' if call_type == 'callee' else 'outbound',
            call_type=call_type,
            ari_control= False,
            status='bridged',
            channel= ChannelData(
                id=unique,
                name=message.get('Channel'),
                state=message.get('ChannelStateDesc'),
                protocol_id=unique,
                caller=CallerData(
                    name=message.get('CallerIDName', ''),
                    number=message.get('CallerIDNum', '')
                ),
                accountcode=message.get('AccountCode'),
                connected=ConnectedData(
                    name=message.get('ConnectedIDName', ''),
                    number=message.get('ConnectedIDNum', '')
                ),
                dialplan=DialplanData(
                    exten=message.get('Exten', ''),
                    context=message.get('Context'),
                    priority=message.get('Priority'),
                )
            ),
            call_start_time=timezone.now(),
            call_answer_time=timezone.now(),
        )
    def create_session_from_ari_event(self, tenant_id:int , event_data: Dict[str, Any]) -> Optional[CallSessionData]:
        data = None
        if 'channel' in event_data:
            data = event_data['channel']
        elif 'peer' in event_data:
            data = event_data['peer']

        if data:
            channel = from_dict(
                data_class=ChannelData,
                data=event_data["channel"],
                config=Config(type_hooks={datetime: lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")})
            )

            return CallSessionData(
                session_id=channel.protocol_id,
                tenant_id=tenant_id,
                asterisk_id=event_data['asterisk_id'],
                asterisk_host=event_data['asterisk_host'],
                rtp_endpoint_host=event_data['rtp_endpoint_host'],
                direction='outbound' if self.is_inbound_channel(channel.name) else 'inbound',
                ari_control= True,
                channel=channel,
                status='answered',
                call_start_time=timezone.now(),
            )
        return None

    @staticmethod
    def is_inbound_channel(channel: str) -> bool:
        """Determine if a channel represents an inbound call."""
        # This is a simplified heuristic - in production, you'd use
        # more sophisticated logic based on your Asterisk configuration
        inbound_technologies = ['PJSIP', 'SIP', 'DAHDI', 'IAX2']
        outbound_technologies = ['Local']

        if '/' in channel:
            tech = channel.split('/')[0]
            if tech in outbound_technologies:
                return False
            if tech in inbound_technologies:
                return True

        # Default to Outbound for unknown channels
        return False

    async def create_session(self, session_data: CallSessionData, store_database: bool = True) -> str:
        """
        Create new call session with early detection from AMI events
        """
        try:
            session_id = session_data.session_id or str(uuid.uuid4())
            session_data.session_id = session_id
            session_data.updated_at = timezone.now()

            session_key = self._get_session_key(session_id)
            tenant_sessions_key = self._get_tenant_session_key(session_data.tenant_id)
            session_json = json.dumps(asdict(session_data), default=str)

            # ✅ create pipeline (no async with)
            pipe = self.redis_client.pipeline()

            pipe.setex(session_key, self.SESSION_EXPIRY, session_json)
            pipe.sadd(tenant_sessions_key, session_id)
            pipe.expire(tenant_sessions_key, self.SESSION_EXPIRY)

            if session_data.channel.id:
                channel_key = self._get_channel_session_key(session_data.channel.id)
                pipe.setex(channel_key, self.SESSION_EXPIRY, session_id)

            # ✅ only execute is awaited
            pipe.execute()

            logger.info(f"Created session {session_id} for tenant {session_data.tenant_id}")
            if store_database:
                await self._create_database_session(session_data)

            return session_id

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    @staticmethod
    async def _create_database_session(session_data: CallSessionData):
        """Create persistent database record for session."""
        try:
            await asyncio.to_thread(
                lambda: CallSession.objects.create(
                    tenant_id=session_data.tenant_id,
                    asterisk_channel_id=session_data.channel.id,
                    asterisk_unique_id=session_data.channel.protocol_id,
                    channel_name=session_data.channel.name,
                    channel_id=session_data.channel.id,
                    caller_id_name=session_data.channel.caller.number or '',
                    caller_id_number=session_data.channel.caller.name or '',
                    dialed_number=session_data.channel.dialplan.exten or '',
                    snoop_channel_id=session_data.snoop_channel_id,
                    external_media_channel_id=session_data.external_media_channel_id,
                    call_type=session_data.call_type,
                    bridge_id=session_data.bridge_id,
                    rtp_endpoint_host=session_data.rtp_endpoint_host,
                    rtp_endpoint_port=session_data.rtp_endpoint_port,
                    session_metadata=session_data.metadata,
                    direction=session_data.direction,
                    status=session_data.status,
                    call_start_time=session_data.call_start_time,
                    call_answer_time=session_data.call_answer_time,
                    call_end_time=session_data.call_end_time,
                )
            )
        except Exception as e:
            logger.error(f"Error creating database session: {e}")

    @staticmethod
    async def _update_database_session(sessionId, updates: Dict[str, Any]):
        """Update persistent database record for session."""
        try:
            allowed_fields = set(f.name for f in CallSession._meta.get_fields())
            safe_updates = {k: v for k, v in updates.items() if k in allowed_fields}

            if 'metadata' in updates:
                safe_updates['session_metadata'] = updates.pop('metadata')

            # logger.info(f"Updating session {sessionId} with updates: {safe_updates}")
            await asyncio.to_thread(
                lambda: CallSession.objects.filter(
                    asterisk_unique_id=sessionId
                ).update(**safe_updates)
            )
        except Exception as e:
            logger.error(f"Error updating database session: {e}")

    async def get_session(self, session_id: str) -> Optional[CallSessionData]:
        """Retrieve session data by session ID"""
        try:
            session_key = self._get_session_key(session_id)
            session_json = self.redis_client.get(session_key)

            if not session_json:
                return None

            session_dict = json.loads(session_json)

            # Convert datetime strings back to datetime objects
            for time_field in ['call_start_time', 'call_answer_time', 'call_end_time', 'created_at', 'updated_at']:
                if session_dict.get(time_field):
                    session_dict[time_field] = datetime.fromisoformat(session_dict[time_field])

            channel = from_dict(
                data_class=ChannelData,
                data=session_dict["channel"],
                config=Config(type_hooks={datetime: lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f%z")})
            )

            del session_dict["channel"]
            return CallSessionData(channel=channel, **session_dict)

        except Exception as e:
            logger.error(f"Failed to retrieve session {session_id}: {e}")
            return None

    async def get_session_by_channel(self, channel_id: str) -> Optional[CallSessionData]:
        """Retrieve session data by Asterisk channel ID"""
        try:
            channel_key = self._get_channel_session_key(channel_id)
            session_id = self.redis_client.get(channel_key)

            if not session_id:
                return None

            return await self.get_session(session_id)

        except Exception as e:
            logger.error(f"Failed to retrieve session by channel {channel_id}: {e}")

    async def update_session(self, session_id: str, updates: Dict[str, Any], update_database:bool = True) -> bool:
        """Update existing session with new data"""
        try:
            # Get current session data
            current_session = await self.get_session(session_id)
            if not current_session:
                logger.warning(f"Session {session_id} not found for update")
                return False

            # Update fields
            for field, value in updates.items():
                if hasattr(current_session, field):
                    setattr(current_session, field, value)

            current_session.updated_at = timezone.now()

            # Store updated session
            session_key = self._get_session_key(session_id)
            session_json = json.dumps(asdict(current_session), default=str)
            self.redis_client.setex(session_key, self.SESSION_EXPIRY, session_json)

            logger.debug(f"Updated session {session_id}")
            if update_database:
                await self._update_database_session(session_id, updates)
            return True

        except Exception as e:
            logger.error(f"Failed to update session {session_id}: {e}")
            return False

    async def get_tenant_sessions(self, tenant_id: int, status_filter: Optional[str] = None) -> List[CallSessionData]:
        """Get all sessions for a specific tenant with optional status filter"""
        try:
            tenant_sessions_key = self._get_tenant_session_key(tenant_id)
            session_ids = self.redis_client.smembers(tenant_sessions_key)

            sessions = []
            for session_id in session_ids:
                session = await self.get_session(session_id)
                if session and (not status_filter or session.status == status_filter):
                    sessions.append(session)

            # Sort by creation time (newest first)
            sessions.sort(key=lambda s: s.created_at or timezone.now(), reverse=True)
            return sessions

        except Exception as e:
            logger.error(f"Failed to retrieve tenant sessions for {tenant_id}: {e}")
            return []

    async def cleanup_session(self, session_id: str) -> bool:
        """Clean up session data and associated mappings."""
        try:
            session = await self.get_session(session_id)
            if not session:
                return False

            session_key = self._get_session_key(session_id)
            tenant_sessions_key = self._get_tenant_session_key(session.tenant_id)

            # ✅ create pipeline
            pipe = self.redis_client.pipeline()

            pipe.delete(session_key)
            pipe.srem(tenant_sessions_key, session_id)


            if session.channel.id:
                channel_key = self._get_channel_session_key(session.channel.id)
                pipe.delete(channel_key)

            # ✅ execute once
            pipe.execute()

            logger.info(f"Cleaned up session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to cleanup session {session_id}: {e}")
            return False

    async def expire_old_sessions(self, max_age_hours: int = 24) -> int:
        """Clean up sessions older than specified age"""
        try:
            cutoff_time = timezone.now() - timedelta(hours=max_age_hours)
            expired_count = 0

            # Get all session keys
            session_keys = self.redis_client.keys(f"{self.SESSION_PREFIX}:*")

            for session_key in session_keys:
                session_json = self.redis_client.get(session_key)
                if session_json:
                    session_dict = json.loads(session_json)
                    created_at_str = session_dict.get('created_at')
                    if created_at_str:
                        created_at = datetime.fromisoformat(created_at_str)
                        if created_at < cutoff_time:
                            session_id = session_key.split(':')[1]
                            await self.cleanup_session(session_id)
                            expired_count += 1

            logger.info(f"Expired {expired_count} old sessions")
            return expired_count

        except Exception as e:
            logger.error(f"Failed to expire old sessions: {e}")
            return 0

    async def get_session_stats(self, tenant_id: Optional[int] = None) -> Dict[str, Any]:
        """Get session statistics for monitoring"""
        try:
            stats = {
                'total_sessions': 0,
                'active_sessions': 0,
                'sessions_by_status': {},
                'sessions_by_tenant': {}
            }

            if tenant_id:
                # Get stats for specific tenant
                sessions = await self.get_tenant_sessions(tenant_id)
                stats['total_sessions'] = len(sessions)

                for session in sessions:
                    if session.status in ['detected', 'answered', 'bridged', 'recording']:
                        stats['active_sessions'] += 1

                    status_count = stats['sessions_by_status'].get(session.status, 0)
                    stats['sessions_by_status'][session.status] = status_count + 1

            else:
                # Get global stats
                session_keys = self.redis_client.keys(f"{self.SESSION_PREFIX}:*")
                stats['total_sessions'] = len(session_keys)

                # This would be expensive for large numbers of sessions
                # In production, consider maintaining counters

            return stats

        except Exception as e:
            logger.error(f"Failed to get session stats: {e}")
            return {}

# Singleton instance for global access
_session_manager = None

def get_session_manager() -> SessionManager:
    """Get singleton SessionManager instance"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
