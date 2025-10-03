# Author: RA
# Purpose: Asterisk REST Interface Client Manager
# Created: 24/09/2025
import asyncio
import logging
from datetime import timedelta
from typing import Callable, Dict, List, Optional
from core.models import Tenant
from ari import ARIConfig, ARIClient, CircuitBreaker
from ari.exceptions import ConnectionError
from core.session.manager import get_session_manager, SessionManager
from django.utils import timezone
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ConnectionStats:
    """Statistics for AMI connections."""
    connected_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    connection_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    events_processed: int = 0
    reconnection_count: int = 0
    last_error: Optional[str] = None
    connected: bool = False
    failed_operations: int = 0
    reconnect_count: int = 0

class ARIConnection:

    def __init__(self, tenant_id: int, config: ARIConfig):
        self.tenant_id = tenant_id
        self.config = config
        self.client = ARIClient(self.config)
        self.stats = ConnectionStats()
        self.event_handlers: Dict[str, Callable] = {}
        self.reconnect_task = None
        self.health_check_task = None
        self.session_manager = get_session_manager()
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._lock = asyncio.Lock()

        # Enhanced error handling and recovery using ari app circuit breaker
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.circuit_breaker_failure_threshold,
            recovery_timeout=self.config.circuit_breaker_recovery_timeout,
            expected_exception=ConnectionError,
            name=f"ARI-{tenant_id}"
        )

        logger.info(f"ARI connection initialized for tenant {tenant_id}")

    async def connect(self):
        async with self._lock:
            if self.stats.connected:
                logger.debug(f"ARI already connected for tenant {self.tenant_id}")
                return True

            retry_count = 0
            while retry_count < self.config.retry_attempts:
                try:
                    logger.info(f"Attempting ARI connection for tenant {self.tenant_id} (attempt {retry_count + 1})")

                    await self.client.connect()

                    # Update connection stats
                    self.stats.connected = True
                    self.stats.connection_time = timezone.now()
                    self.stats.last_activity = timezone.now()

                    logger.info(f"ARI connected successfully for tenant {self.tenant_id}")
                    return True
                except Exception as e:
                    retry_count += 1
                    self.stats.failed_operations += 1
                    logger.error(f"ARI connection failed for tenant {self.tenant_id}: {e}")

                    if retry_count < self.config.retry_attempts:
                        logger.info(f"Retrying in {self.config.retry_delay} seconds...")
                        await asyncio.sleep(self.config.retry_delay)
                    else:
                        logger.error(f"Max retries reached for tenant {self.tenant_id}")
                        await self._schedule_reconnect()
                        return False
            return False

    async def connect_websocket(self):
        await self.client.connect_websocket()

    async def disconnect(self):
        """Gracefully disconnect ARI client"""
        async with self._lock:
            if not self.stats.connected:
                return

            try:
                # Cancel background tasks
                if self.reconnect_task:
                    self.reconnect_task.cancel()
                if self.health_check_task:
                    self.health_check_task.cancel()

                # Close ARI connection
                if self.client.is_connected:
                    await self.client.close()
                    self.client = None

                self.stats.connected = False
                logger.info(f"ARI disconnected for tenant {self.tenant_id}")

            except Exception as e:
                logger.error(f"Error disconnecting ARI for tenant {self.tenant_id}: {e}")

    async def _schedule_reconnect(self):
        """Schedule automatic reconnection"""
        if self.reconnect_task and not self.reconnect_task.done():
            return

        self.reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Background reconnection loop"""
        while not self.stats.connected:
            try:
                logger.info(f"Attempting to reconnect ARI for tenant {self.tenant_id}")
                self.stats.reconnect_count += 1

                if await self.connect():
                    logger.info(f"ARI reconnected successfully for tenant {self.tenant_id}")
                    break

                # Wait before next attempt
                await asyncio.sleep(self.config.retry_delay * 2)  # Longer delay for reconnects

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reconnection error for tenant {self.tenant_id}: {e}")
                await asyncio.sleep(self.config.retry_delay)

    def register_event_handler(self, event_type: str, handler: Callable):
        """Register event handler for specific ARI event type."""
        if not self.client:
            return

        self.client.on_event(event_type, handler)


class ARIManager:
    """Multi-tenant ARI connection manager with connection pooling."""

    def __init__(self):
        self.connections: Dict[int, ARIConnection] = {}
        self.session_manager: SessionManager = get_session_manager()
        self._lock = asyncio.Lock()

    async def get_connection(self, tenant_id: int) -> Optional[ARIConnection]:
        """Get or create AMI connection for tenant."""
        async with self._lock:
            # Return existing connection if available and healthy
            if tenant_id in self.connections:
                connection = self.connections[tenant_id]
                return connection

            # Create new connection
            config = await self._get_tenant_ari_config(tenant_id)
            if not config:
                return None

            connection = ARIConnection(tenant_id, config)
            if await connection.connect():
                self.connections[tenant_id] = connection
                return connection
            return None

    async def disconnect_tenant(self, tenant_id: int):
        """Disconnect ARI connection for specific tenant."""
        async with self._lock:
            if tenant_id in self.connections:
                await self.connections[tenant_id].disconnect()
                del self.connections[tenant_id]

    async def disconnect_all(self):
        """Disconnect all ARI connections."""
        async with self._lock:
            for connection in self.connections.values():
                await connection.disconnect()
            self.connections.clear()

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
            health_status[tenant_id] = connection.stats.connected

            # Check if connection is stale
            if connection.stats.last_activity:
                time_since_event = timezone.now() - connection.stats.last_event_at
                if time_since_event > timedelta(minutes=5):  # 5 minutes threshold
                    logger.warning(f"No AMI events received for tenant {tenant_id} in {time_since_event}")
                    health_status[tenant_id] = False

        return health_status

    @staticmethod
    async def _get_tenant_ari_config(tenant_id: int) -> Optional[ARIConfig]:
        """Get ARI configuration for tenant from database."""
        try:
            # Get tenant
            tenant = await asyncio.to_thread(
                Tenant.objects.get,
                id=tenant_id
            )

            ari_config = ARIConfig(
                host=tenant.host,
                port=tenant.ari_port,
                username=tenant.ari_username,
                password=tenant.ari_password,
                app_name=tenant.ari_app_name
            )

            if not ari_config.username or not ari_config.password:
                logger.error(f"ARI credentials not configured for tenant {tenant_id}")
                return None

            return ari_config

        except Tenant.DoesNotExist:
            logger.error(f"Tenant {tenant_id} not found")
            return None
        except Exception as e:
            logger.error(f"Error getting AMI config for tenant {tenant_id}: {e}")
            return None

# Global ARI manager instance
_ari_manager = None

def get_ari_manager() -> ARIManager:
    """Get global ARI manager instance (singleton pattern)"""
    global _ari_manager
    if _ari_manager is None:
        _ari_manager = ARIManager()
    return _ari_manager

def cleanup_ari_manager():
    """Cleanup global ARI manager"""
    global _ari_manager
    if _ari_manager:
        try:
            # Try to create task if event loop is running
            loop = asyncio.get_running_loop()
            loop.create_task(_ari_manager.disconnect_all())
        except RuntimeError:
            # No event loop running, cleanup without disconnection
            logger.debug("No event loop running, cleaning up ARI manager without disconnection")
        _ari_manager = None
