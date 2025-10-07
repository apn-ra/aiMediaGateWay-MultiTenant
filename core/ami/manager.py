# Author: RA
# Purpose: Asterisk Manager Client with Panoramisk
# Created: 23/09/2025

import logging
import asyncio

from asgiref.sync import sync_to_async
from rest_framework import serializers
from core.models import Tenant
from core.session.manager import SessionManager, get_session_manager
from core.junie_codes.ami.ami_manager import AMIConnectionConfig, ConnectionStats
from typing import Dict, List, Optional, Any, Callable
from datetime import timedelta
import panoramisk

from django.utils import timezone

logger = logging.getLogger(__name__)

class AMIConnection:
    def __init__(self, tenant_id: int, config: AMIConnectionConfig):
        self._task = None
        self.tenant_id = tenant_id
        self.config = config
        self.stats = ConnectionStats()
        self.manager: Optional[panoramisk.Manager] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._lock = asyncio.Lock()

        self.initialize()

    def initialize(self):
        logger.info(f"Initializing to AMI for tenant {self.tenant_id}")

        self.manager = panoramisk.Manager(
            host=self.config.host,
            port=self.config.port,
            username=self.config.username,
            secret=self.config.secret,
            timeout=self.config.timeout
        )

    async def _setup_event_handlers(self):
        """Setup internal event handlers for connection monitoring."""

        # Register event handler for all events to update statistics
        self.manager.register_event('*', self._handle_any_event)

        # Register specific event handlers
        for event_type, handlers in self._event_handlers.items():
            for handler in handlers:
                self.manager.register_event(event_type, handler)

    def _handle_any_event(self, manager, event):
        """Handle any AMI event to update statistics."""
        self.stats.last_event_at = timezone.now()
        self.stats.events_processed += 1

        # ev = event.get('Event')
        #logger.info(f"Event: {ev}")

    def register_event_handler(self, event_type: str, handler: Callable):
        """Register event handler for specific AMI event type."""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def test_connect(self):
        try:
            self.manager.connect(run_forever=False)
            return True
        except Exception as e:
            logger.error(f"AMI connection test failed: {e}")
            raise serializers.ValidationError(str(e))

    async def connect(self):
        async with self._lock:
            try:
                if self.manager and self.stats.is_healthy:
                    return True

                logger.info(f"Connecting to AMI for tenant {self.tenant_id}")

                await self._setup_event_handlers()

                self.manager.connect(run_forever=False)

                # Update statistics
                self.stats.connected_at = timezone.now()
                self.stats.is_healthy = True
                self.stats.last_error = None

                # Start ping task for connection monitoring
                await self._start_ping_task()

                logger.info(f"AMI connected successfully for tenant {self.tenant_id}")
                return True

            except Exception as e:
                error_msg = f"AMI connection failed for tenant {self.tenant_id}: {str(e)}"
                logger.error(error_msg)
                self.stats.last_error = error_msg
                self.stats.is_healthy = False

                # Schedule reconnection
                await self._schedule_reconnect()
                return False

    async def disconnect(self):
        """Gracefully disconnect AMI connection."""
        async with self._lock:
            if self._ping_task:
                self._ping_task.cancel()

            if self._reconnect_task:
                self._reconnect_task.cancel()

            if self.manager:
                try:
                    self.manager.close()
                except Exception as e:
                    logger.warning(f"Error closing AMI connection for tenant {self.tenant_id}: {e}")
                finally:
                    self.manager = None

            self.stats.is_healthy = False
            logger.info(f"AMI disconnected for tenant {self.tenant_id}")

    async def send_command(self, action: dict, **kwargs) -> Optional[Dict[str, Any]]:
        """Send AMI command with error handling."""
        if not self.manager or not self.stats.is_healthy:
            logger.warning(f"AMI not connected for tenant {self.tenant_id}")
            return None

        try:
            response = await self.manager.send_action(action, **kwargs)
            return response
        except Exception as e:
            logger.error(f"AMI command failed for tenant {self.tenant_id}: {e}")
            self.stats.last_error = str(e)
            await self._schedule_reconnect()
            return None

    async def _start_ping_task(self):
        """Start periodic ping task to monitor connection health."""
        if self._ping_task:
            self._ping_task.cancel()

        self._ping_task = asyncio.create_task(self._ping_loop())

    async def _ping_loop(self):
        """Periodic ping to check connection health."""
        while self.stats.is_healthy:
            try:
                await asyncio.sleep(self.config.ping_interval)

                if not self.manager:
                    break

                # Send ping command
                response = await self.send_command({
                    'Action': 'Ping',
                    'ActionID': 'my-ping-1'
                })

                if not response or response.get('Response') != 'Success':
                    logger.warning(f"AMI ping failed for tenant {self.tenant_id}")
                    await self._schedule_reconnect()
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AMI ping error for tenant {self.tenant_id}: {e}")
                await self._schedule_reconnect()
                break

    async def _schedule_reconnect(self):
        """Schedule automatic reconnection."""
        if self._reconnect_task and not self._reconnect_task.done():
            return

        self.stats.is_healthy = False
        self.stats.reconnection_count += 1

        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Automatic reconnection loop with exponential backoff."""
        retry_count = 0

        while retry_count < self.config.max_retries:
            try:
                # Wait before retry with exponential backoff
                delay = self.config.retry_delay * (2 ** retry_count)
                await asyncio.sleep(min(delay, 60))  # Max 60 seconds

                logger.info(f"Attempting AMI reconnection for tenant {self.tenant_id} (attempt {retry_count + 1})")

                if await self.connect():
                    logger.info(f"AMI reconnected successfully for tenant {self.tenant_id}")
                    return

                retry_count += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AMI reconnection error for tenant {self.tenant_id}: {e}")
                retry_count += 1

        logger.error(f"AMI reconnection failed after {self.config.max_retries} attempts for tenant {self.tenant_id}")


class AMIManager:
    """Multi-tenant AMI connection manager with connection pooling."""

    def __init__(self):
        self.connections: Dict[int, AMIConnection] = {}
        self.session_manager: SessionManager = get_session_manager()
        self._lock = asyncio.Lock()

    async def get_connection(self, tenant_id: int) -> Optional[AMIConnection]:
        """Get or create AMI connection for tenant."""
        # Return existing connection if available and healthy
        if tenant_id in self.connections:
            connection = self.connections[tenant_id]
            return connection

        # Create new connection
        config = await self._get_tenant_ami_config(tenant_id)
        if not config:
            return None

        connection = AMIConnection(tenant_id, config)
        self.connections[tenant_id] = connection
        return connection


    async def disconnect_tenant(self, tenant_id: int):
        """Disconnect AMI connection for specific tenant."""
        async with self._lock:
            if tenant_id in self.connections:
                await self.connections[tenant_id].disconnect()
                del self.connections[tenant_id]

    async def disconnect_all(self):
        """Disconnect all AMI connections."""
        async with self._lock:
            for connection in self.connections.values():
                await connection.disconnect()
            self.connections.clear()

    async def send_command(self, tenant_id: int, action: dict, **kwargs) -> Optional[Dict[str, Any]]:
        """Send AMI command for specific tenant."""
        connection = await self.get_connection(tenant_id)
        if connection.stats.is_healthy:
            return await connection.send_command(action, **kwargs)
        return None

    async def register_event_handler(self, tenant_id: int, event_type: str, handler: Callable):
        """Register event handler for specific tenant."""
        connection = await self.get_connection(tenant_id)
        if connection:
            connection.register_event_handler(event_type, handler)

    def get_connection_stats(self) -> Dict[int, ConnectionStats]:
        """Get statistics for all connections."""
        return {
            tenant_id: connection.stats
            for tenant_id, connection in self.connections.items()
        }

    async def health_check(self) -> Dict[str, bool]:
        """Perform health check on all connections."""
        health_status = {}

        for tenant_id, connection in self.connections.items():
            health_status[tenant_id] = connection.stats.is_healthy

            # Check if connection is stale
            if connection.stats.last_event_at:
                time_since_event = timezone.now() - connection.stats.last_event_at
                if time_since_event > timedelta(minutes=5):  # 5 minutes threshold
                    logger.warning(f"No AMI events received for tenant {tenant_id} in {time_since_event}")
                    health_status[tenant_id] = False

        return health_status

    async def _get_tenant_ami_config(self, tenant_id: int) -> Optional[AMIConnectionConfig]:
        """Get AMI configuration for tenant from database."""
        try:
            # Get tenant
            tenant = await asyncio.to_thread(
                Tenant.objects.get,
                id=tenant_id
            )

            ami_config = AMIConnectionConfig(
                host=tenant.host,
                port=tenant.ami_port,
                username=tenant.ami_username,
                secret=tenant.ami_secret,
                timeout=30.0,
                max_retries=3,
                retry_delay=5.0,
                ping_interval=30.0
            )

            if not ami_config.username or not ami_config.secret:
                logger.error(f"AMI credentials not configured for tenant {tenant_id}")
                return None

            return ami_config

        except Tenant.DoesNotExist:
            logger.error(f"Tenant {tenant_id} not found")
            return None
        except Exception as e:
            logger.error(f"Error getting AMI config for tenant {tenant_id}: {e}")
            return None


# Global AMI manager instance
_ami_manager: Optional[AMIManager] = None


async def get_ami_manager() -> AMIManager:
    """Get or create global AMI manager instance."""
    global _ami_manager
    if _ami_manager is None:
        _ami_manager = AMIManager()
    return _ami_manager


async def cleanup_ami_manager():
    """Cleanup global AMI manager instance."""
    global _ami_manager
    if _ami_manager:
        await _ami_manager.disconnect_all()
        _ami_manager = None
