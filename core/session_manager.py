"""
Session Management System for aiMediaGateway
Implements session-first approach with Redis backend for multi-tenant call tracking.
"""

import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from django.conf import settings
from django.utils import timezone
import redis


logger = logging.getLogger(__name__)


@dataclass
class CallSessionData:
    """Data class for call session information"""
    session_id: str
    tenant_id: str
    asterisk_channel_id: str
    asterisk_unique_id: str
    caller_id_name: Optional[str] = None
    caller_id_number: Optional[str] = None
    dialed_number: Optional[str] = None
    direction: str = 'inbound'  # 'inbound' or 'outbound'
    status: str = 'detected'
    bridge_id: Optional[str] = None
    external_media_channel_id: Optional[str] = None
    rtp_endpoint_host: Optional[str] = None
    rtp_endpoint_port: Optional[int] = None
    session_metadata: Dict[str, Any] = None
    call_start_time: Optional[datetime] = None
    call_answer_time: Optional[datetime] = None
    call_end_time: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        if self.session_metadata is None:
            self.session_metadata = {}
        if self.created_at is None:
            self.created_at = timezone.now()
        if self.updated_at is None:
            self.updated_at = timezone.now()


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

    def _get_tenant_session_key(self, tenant_id: str) -> str:
        """Generate Redis key for tenant session index"""
        return f"{self.TENANT_PREFIX}:{tenant_id}:sessions"

    def _get_channel_session_key(self, channel_id: str) -> str:
        """Generate Redis key for channel to session mapping"""
        return f"{self.CHANNEL_PREFIX}:{channel_id}"

    async def create_session(self, session_data: CallSessionData) -> str:
        """
        Create new call session with early detection from AMI events
        """
        try:
            session_id = session_data.session_id or str(uuid.uuid4())
            session_data.session_id = session_id
            session_data.updated_at = timezone.now()

            # Store session data
            session_key = self._get_session_key(session_id)
            session_json = json.dumps(asdict(session_data), default=str)
            
            # Use Redis pipeline for atomic operations
            pipe = self.redis_client.pipeline()
            
            # Store session data with expiration
            pipe.setex(session_key, self.SESSION_EXPIRY, session_json)
            
            # Add session to tenant index
            tenant_sessions_key = self._get_tenant_session_key(session_data.tenant_id)
            pipe.sadd(tenant_sessions_key, session_id)
            pipe.expire(tenant_sessions_key, self.SESSION_EXPIRY)
            
            # Create channel to session mapping
            if session_data.asterisk_channel_id:
                channel_key = self._get_channel_session_key(session_data.asterisk_channel_id)
                pipe.setex(channel_key, self.SESSION_EXPIRY, session_id)
            
            # Execute pipeline
            pipe.execute()

            logger.info(f"Created session {session_id} for tenant {session_data.tenant_id}")
            return session_id

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

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

            return CallSessionData(**session_dict)

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
            return None

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
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
            return True

        except Exception as e:
            logger.error(f"Failed to update session {session_id}: {e}")
            return False

    async def get_tenant_sessions(self, tenant_id: str, status_filter: Optional[str] = None) -> List[CallSessionData]:
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
        """Clean up session data and associated mappings"""
        try:
            # Get session data first
            session = await self.get_session(session_id)
            if not session:
                return False

            # Use Redis pipeline for atomic cleanup
            pipe = self.redis_client.pipeline()

            # Remove session data
            session_key = self._get_session_key(session_id)
            pipe.delete(session_key)

            # Remove from tenant index
            tenant_sessions_key = self._get_tenant_session_key(session.tenant_id)
            pipe.srem(tenant_sessions_key, session_id)

            # Remove channel mapping
            if session.asterisk_channel_id:
                channel_key = self._get_channel_session_key(session.asterisk_channel_id)
                pipe.delete(channel_key)

            # Execute cleanup
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

    async def get_session_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
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
