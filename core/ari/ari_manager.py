"""
ARI (Asterisk REST Interface) Client Manager

This module provides a connection manager for ARI clients with support for:
- Multi-tenant connection pooling
- Automatic reconnection and failover
- Health monitoring and statistics
- Call control operations
- Integration with session management

Following the same architectural patterns as ami_manager.py for consistency.
"""

import asyncio
import logging
from ari import ARIClient, ARIConfig
from typing import Dict, Optional, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from django.conf import settings
from django.utils import timezone


from core.models import Tenant, SystemConfiguration
from core.session.session_manager import get_session_manager

logger = logging.getLogger(__name__)


class ARIErrorType(Enum):
    """Categorization of ARI operation errors for appropriate recovery strategies"""
    CONNECTION_ERROR = "connection_error"          # Connection lost, network issues
    AUTHENTICATION_ERROR = "authentication_error"  # Invalid credentials
    TIMEOUT_ERROR = "timeout_error"               # Operation timed out
    RESOURCE_NOT_FOUND = "resource_not_found"     # Channel/Bridge not found
    INVALID_STATE = "invalid_state"               # Operation not valid in current state
    PERMISSION_DENIED = "permission_denied"       # Insufficient permissions
    RATE_LIMITED = "rate_limited"                 # Too many requests
    SERVER_ERROR = "server_error"                 # Asterisk server error
    UNKNOWN_ERROR = "unknown_error"               # Unrecognized error


class ARICircuitBreaker:
    """Circuit breaker pattern for ARI operations to prevent cascade failures"""
    
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.is_open = False
        
    def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        if self.is_open:
            if self._should_attempt_reset():
                return self._attempt_reset(func, *args, **kwargs)
            else:
                raise Exception("Circuit breaker is open - operation blocked")
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise
    
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if not self.last_failure_time:
            return True
        return (timezone.now() - self.last_failure_time).seconds >= self.recovery_timeout
    
    def _attempt_reset(self, func, *args, **kwargs):
        """Attempt to reset circuit breaker with test call"""
        try:
            result = func(*args, **kwargs)
            self._on_success()
            logger.info("Circuit breaker reset successfully")
            return result
        except Exception as e:
            self._on_failure()
            raise
    
    def _on_success(self):
        """Handle successful operation"""
        self.failure_count = 0
        self.is_open = False
        self.last_failure_time = None
    
    def _on_failure(self):
        """Handle failed operation"""
        self.failure_count += 1
        self.last_failure_time = timezone.now()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            logger.warning(f"Circuit breaker opened after {self.failure_count} failures")


class ARIRetryConfig:
    """Configuration for retry logic with exponential backoff"""
    
    def __init__(self, max_attempts: int = 3, base_delay: float = 1.0, 
                 max_delay: float = 60.0, backoff_factor: float = 2.0):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number"""
        delay = self.base_delay * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay)

@dataclass
class ARIConnectionConfig:
    """Configuration for ARI connection"""
    host: str
    port: int
    username: str
    password: str
    app_name: str = "aiMediaGateway"
    timeout: int = 30
    max_retries: int = 3
    retry_delay: int = 5


@dataclass
class ARIConnectionStats:
    """Statistics for ARI connection monitoring"""
    connected: bool = False
    connection_time: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    total_operations: int = 0
    failed_operations: int = 0
    reconnect_count: int = 0
    # Enhanced error tracking for better recovery decisions
    connection_errors: int = 0
    timeout_errors: int = 0
    authentication_errors: int = 0
    server_errors: int = 0
    circuit_breaker_trips: int = 0
    retry_attempts: int = 0
    last_error_type: Optional[ARIErrorType] = None
    last_error_time: Optional[datetime] = None


class ARIConnection:
    """
    Individual ARI connection for a specific tenant
    Handles connection management, operations, and health monitoring
    """
    
    def __init__(self, tenant_id: str, config: ARIConfig):
        self.tenant_id = tenant_id
        self.config = config
        self.client = ARIClient(self.config)
        self.stats = ARIConnectionStats()
        self.event_handlers: Dict[str, Callable] = {}
        self.reconnect_task = None
        self.health_check_task = None
        self.session_manager = get_session_manager()
        self._lock = asyncio.Lock()
        
        # Enhanced error handling and recovery
        self.circuit_breaker = ARICircuitBreaker(
            failure_threshold=getattr(settings, 'ARI_CIRCUIT_BREAKER_THRESHOLD', 5),
            recovery_timeout=getattr(settings, 'ARI_CIRCUIT_BREAKER_TIMEOUT', 60)
        )
        self.retry_config = ARIRetryConfig(
            max_attempts=getattr(settings, 'ARI_MAX_RETRY_ATTEMPTS', 3),
            base_delay=getattr(settings, 'ARI_RETRY_BASE_DELAY', 1.0),
            max_delay=getattr(settings, 'ARI_RETRY_MAX_DELAY', 60.0),
            backoff_factor=getattr(settings, 'ARI_RETRY_BACKOFF_FACTOR', 2.0)
        )
        
        logger.info(f"ARI connection initialized for tenant {tenant_id}")

    async def connect(self):
        """Establish ARI connection with automatic retry logic"""
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
                    
                    # Setup event handlers and start health monitoring
                    await self._setup_event_handlers()
                    self._start_health_check_task()
                    
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
                if self.client:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: self.client.close()
                    )
                    self.client = None
                
                self.stats.connected = False
                logger.info(f"ARI disconnected for tenant {self.tenant_id}")
                
            except Exception as e:
                logger.error(f"Error disconnecting ARI for tenant {self.tenant_id}: {e}")

    def _categorize_error(self, error: Exception) -> ARIErrorType:
        """Categorize error for appropriate recovery strategy"""
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()
        
        # Connection-related errors
        if 'connection' in error_str or 'connect' in error_str:
            return ARIErrorType.CONNECTION_ERROR
        if 'timeout' in error_str or 'timed out' in error_str:
            return ARIErrorType.TIMEOUT_ERROR
        if 'network' in error_str or 'unreachable' in error_str:
            return ARIErrorType.CONNECTION_ERROR
            
        # Authentication errors
        if 'unauthorized' in error_str or 'auth' in error_str:
            return ARIErrorType.AUTHENTICATION_ERROR
        if 'permission' in error_str or 'forbidden' in error_str:
            return ARIErrorType.PERMISSION_DENIED
            
        # Resource errors
        if 'not found' in error_str or '404' in error_str:
            return ARIErrorType.RESOURCE_NOT_FOUND
        if 'invalid state' in error_str or 'bad state' in error_str:
            return ARIErrorType.INVALID_STATE
            
        # Rate limiting
        if 'rate limit' in error_str or 'too many' in error_str:
            return ARIErrorType.RATE_LIMITED
            
        # Server errors
        if 'server error' in error_str or '5' in error_str[:2]:
            return ARIErrorType.SERVER_ERROR
            
        return ARIErrorType.UNKNOWN_ERROR

    def _update_error_stats(self, error_type: ARIErrorType):
        """Update error statistics based on error type"""
        self.stats.last_error_type = error_type
        self.stats.last_error_time = timezone.now()
        
        if error_type == ARIErrorType.CONNECTION_ERROR:
            self.stats.connection_errors += 1
        elif error_type == ARIErrorType.TIMEOUT_ERROR:
            self.stats.timeout_errors += 1
        elif error_type == ARIErrorType.AUTHENTICATION_ERROR:
            self.stats.authentication_errors += 1
        elif error_type == ARIErrorType.SERVER_ERROR:
            self.stats.server_errors += 1

    def _should_retry(self, error_type: ARIErrorType, attempt: int) -> bool:
        """Determine if operation should be retried based on error type and attempt count"""
        if attempt >= self.retry_config.max_attempts:
            return False
            
        # Don't retry certain error types
        non_retryable_errors = {
            ARIErrorType.AUTHENTICATION_ERROR,
            ARIErrorType.PERMISSION_DENIED,
            ARIErrorType.RESOURCE_NOT_FOUND
        }
        
        return error_type not in non_retryable_errors

    async def execute_operation(self, operation_func: Callable, *args, **kwargs):
        """
        Execute an ARI operation with enhanced error handling, retry logic, and circuit breaker
        
        Args:
            operation_func: The ARI operation function to execute
            *args: Positional arguments for the operation
            **kwargs: Keyword arguments for the operation
        
        Returns:
            Operation result or None if failed
        """
        if not self.stats.connected or not self.client:
            logger.warning(f"ARI not connected for tenant {self.tenant_id}")
            return None

        # Extract timeout from kwargs or use default
        operation_timeout = kwargs.pop('timeout', getattr(settings, 'ARI_OPERATION_TIMEOUT', 30.0))
        
        for attempt in range(self.retry_config.max_attempts):
            try:
                # Check circuit breaker before attempting operation
                if self.circuit_breaker.is_open and not self.circuit_breaker._should_attempt_reset():
                    logger.warning(f"Circuit breaker is open for tenant {self.tenant_id} - operation blocked")
                    self.stats.circuit_breaker_trips += 1
                    return None
                
                # Execute operation with timeout and circuit breaker protection
                result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, 
                        lambda: self.circuit_breaker.call(operation_func, *args, **kwargs)
                    ),
                    timeout=operation_timeout
                )
                
                # Success - update stats and return result
                self.stats.total_operations += 1
                self.stats.last_activity = timezone.now()
                
                if attempt > 0:
                    logger.info(f"ARI operation succeeded after {attempt + 1} attempts for tenant {self.tenant_id}")
                else:
                    logger.debug(f"ARI operation completed for tenant {self.tenant_id}")
                
                return result
                
            except asyncio.TimeoutError:
                error_type = ARIErrorType.TIMEOUT_ERROR
                self._update_error_stats(error_type)
                self.stats.failed_operations += 1
                
                logger.warning(f"ARI operation timed out (attempt {attempt + 1}/{self.retry_config.max_attempts}) for tenant {self.tenant_id}")
                
                if not self._should_retry(error_type, attempt):
                    logger.error(f"ARI operation failed after timeout - no more retries for tenant {self.tenant_id}")
                    await self._check_connection_health()
                    return None
                    
            except Exception as e:
                # Categorize and handle the error
                error_type = self._categorize_error(e)
                self._update_error_stats(error_type)
                self.stats.failed_operations += 1
                
                logger.warning(f"ARI operation failed (attempt {attempt + 1}/{self.retry_config.max_attempts}) for tenant {self.tenant_id}: {e} (Type: {error_type.value})")
                
                # Check if we should retry
                if not self._should_retry(error_type, attempt):
                    logger.error(f"ARI operation failed with non-retryable error {error_type.value} for tenant {self.tenant_id}")
                    await self._check_connection_health()
                    return None
                    
                # Handle connection errors by triggering health check
                if error_type in [ARIErrorType.CONNECTION_ERROR, ARIErrorType.SERVER_ERROR]:
                    await self._check_connection_health()
            
            # If we reach here, we're retrying - wait with exponential backoff
            if attempt < self.retry_config.max_attempts - 1:
                delay = self.retry_config.get_delay(attempt)
                self.stats.retry_attempts += 1
                logger.info(f"Retrying ARI operation in {delay:.2f}s for tenant {self.tenant_id}")
                await asyncio.sleep(delay)
        
        # All retry attempts exhausted
        logger.error(f"ARI operation failed after {self.retry_config.max_attempts} attempts for tenant {self.tenant_id}")
        await self._check_connection_health()
        return None

    async def answer_call(self, channel_id: str) -> bool:
        """Answer an incoming call"""
        try:
            await self.execute_operation(self.client.answer_channel, channel_id=channel_id)
            logger.info(f"Call answered: {channel_id} for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to answer call {channel_id} for tenant {self.tenant_id}: {e}")
            return False

    async def hangup_call(self, channel_id: str, reason: Optional[str] = None) -> bool:
        """Hangup a call"""
        try:
            await self.execute_operation(self.client.hangup_channel, channel_id=channel_id, reason=reason)
            logger.info(f"Call hung up: {channel_id} for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to hangup call {channel_id} for tenant {self.tenant_id}: {e}")
            return False

    async def create_external_media_channel(self, external_host: str, format: str = "slin16") -> Optional[str]:
        """Create an ExternalMedia channel for RTP streaming"""
        try:
            channel = await self.execute_operation(
                self.client.create_external_media,
                external_host=external_host,
                codec=format,
                app=self.config.app_name
            )
            
            if channel:
                logger.info(f"ExternalMedia channel created: {channel.id} for tenant {self.tenant_id}")
                return channel.id
            return None
            
        except Exception as e:
            logger.error(f"Failed to create ExternalMedia channel for tenant {self.tenant_id}: {e}")
            return None

    async def create_bridge(self, bridge_type: str = "mixing") -> Optional[str]:
        """Create a bridge for connecting channels"""
        try:
            bridge = await self.execute_operation(
                self.client.create_bridge,
                bridge_type=bridge_type
            )
            
            if bridge:
                logger.info(f"Bridge created: {bridge.id} for tenant {self.tenant_id}")
                return bridge.id
            return None
            
        except Exception as e:
            logger.error(f"Failed to create bridge for tenant {self.tenant_id}: {e}")
            return None

    async def add_channel_to_bridge(self, bridge_id: str, channel_id: str) -> bool:
        """Add a channel to a bridge"""
        try:
            bridge = self.client.bridges.get(bridgeId=bridge_id)
            await self.execute_operation(bridge.addChannel, channel=channel_id)
            logger.info(f"Channel {channel_id} added to bridge {bridge_id} for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to add channel to bridge for tenant {self.tenant_id}: {e}")
            return False

    async def start_recording(self, channel_id: str, recording_name: str = None) -> Optional[str]:
        """Start recording a channel"""
        try:
            if not recording_name:
                recording_name = f"recording_{channel_id}_{int(timezone.now().timestamp())}"
            
            channel = await self.client.get_channel(channelId=channel_id)
            recording = await self.execute_operation(
                channel.record,
                name=recording_name,
                format="wav",
                terminateOn="none",
                ifExists="overwrite"
            )
            
            if recording:
                logger.info(f"Recording started: {recording.name} for channel {channel_id} on tenant {self.tenant_id}")
                return recording.name
            return None
            
        except Exception as e:
            logger.error(f"Failed to start recording for channel {channel_id} on tenant {self.tenant_id}: {e}")
            return None

    async def stop_recording(self, recording_name: str) -> bool:
        """Stop a recording"""
        try:
            recording = self.client.recordings.get(recordingName=recording_name)
            await self.execute_operation(recording.stop)
            logger.info(f"Recording stopped: {recording_name} for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to stop recording {recording_name} for tenant {self.tenant_id}: {e}")
            return False

    async def pause_recording(self, recording_name: str) -> bool:
        """Pause a recording"""
        try:
            recording = self.client.recordings.get(recordingName=recording_name)
            await self.execute_operation(recording.pause)
            logger.info(f"Recording paused: {recording_name} for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to pause recording {recording_name} for tenant {self.tenant_id}: {e}")
            return False

    async def resume_recording(self, recording_name: str) -> bool:
        """Resume a paused recording"""
        try:
            recording = self.client.recordings.get(recordingName=recording_name)
            await self.execute_operation(recording.unpause)
            logger.info(f"Recording resumed: {recording_name} for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to resume recording {recording_name} for tenant {self.tenant_id}: {e}")
            return False

    async def terminate_call(self, channel_id: str, reason: str = "normal") -> bool:
        """Terminate a call with specified reason"""
        try:
            channel = self.client.channels.get(channelId=channel_id)
            
            # Set hangup reason if supported
            if reason == "busy":
                await self.execute_operation(channel.setChannelVar, variable="HANGUPCAUSE", value="17")
            elif reason == "congestion":
                await self.execute_operation(channel.setChannelVar, variable="HANGUPCAUSE", value="34")
            elif reason == "no_answer":
                await self.execute_operation(channel.setChannelVar, variable="HANGUPCAUSE", value="19")
            
            # Hangup the channel
            await self.execute_operation(channel.hangup, reason=reason)
            logger.info(f"Call terminated: {channel_id} with reason '{reason}' for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to terminate call {channel_id} for tenant {self.tenant_id}: {e}")
            return False

    async def mute_channel(self, channel_id: str, direction: str = "both") -> bool:
        """Mute a channel (in, out, or both directions)"""
        try:
            channel = self.client.channels.get(channelId=channel_id)
            await self.execute_operation(channel.mute, direction=direction)
            logger.info(f"Channel {channel_id} muted ({direction}) for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to mute channel {channel_id} for tenant {self.tenant_id}: {e}")
            return False

    async def unmute_channel(self, channel_id: str, direction: str = "both") -> bool:
        """Unmute a channel (in, out, or both directions)"""
        try:
            channel = self.client.channels.get(channelId=channel_id)
            await self.execute_operation(channel.unmute, direction=direction)
            logger.info(f"Channel {channel_id} unmuted ({direction}) for tenant {self.tenant_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to unmute channel {channel_id} for tenant {self.tenant_id}: {e}")
            return False

    def register_event_handler(self, event_type: str, handler: Callable):
        """Register event handler for specific ARI events"""
        self.event_handlers[event_type] = handler
        logger.debug(f"Event handler registered for {event_type} on tenant {self.tenant_id}")

    async def _setup_event_handlers(self):
        """Setup ARI event handlers for WebSocket events"""
        if not self.client:
            return
            
        try:
            # Register for application events
            self.client.on_event('StasisStart', self._handle_stasis_start)
            self.client.on_event('StasisEnd', self._handle_stasis_end)
            self.client.on_event('ChannelStateChange', self._handle_channel_state_change)
            
            logger.debug(f"ARI event handlers setup for tenant {self.tenant_id}")
        except Exception as e:
            logger.error(f"Failed to setup event handlers for tenant {self.tenant_id}: {e}")

    async def _handle_stasis_start(self, channel, event):
        """Handle StasisStart events (channel enters application)"""
        try:
            logger.info(f"Channel {channel.id} entered application for tenant {self.tenant_id}")
            
            # Custom event handler
            if 'StasisStart' in self.event_handlers:
                await self.event_handlers['StasisStart'](channel, event)
                
        except Exception as e:
            logger.error(f"Error handling StasisStart for tenant {self.tenant_id}: {e}")

    async def _handle_stasis_end(self, channel, event):
        """Handle StasisEnd events (channel leaves application)"""
        try:
            logger.info(f"Channel {channel.id} left application for tenant {self.tenant_id}")
            
            # Custom event handler
            if 'StasisEnd' in self.event_handlers:
                await self.event_handlers['StasisEnd'](channel, event)
                
        except Exception as e:
            logger.error(f"Error handling StasisEnd for tenant {self.tenant_id}: {e}")

    async def _handle_channel_state_change(self, channel, event):
        """Handle channel state changes"""
        try:
            logger.debug(f"Channel {channel.id} state changed to {channel.state} for tenant {self.tenant_id}")
            
            # Custom event handler
            if 'ChannelStateChange' in self.event_handlers:
                await self.event_handlers['ChannelStateChange'](channel, event)
                
        except Exception as e:
            logger.error(f"Error handling ChannelStateChange for tenant {self.tenant_id}: {e}")

    def _start_health_check_task(self):
        """Start background health check task"""
        if self.health_check_task:
            self.health_check_task.cancel()
        self.health_check_task = asyncio.create_task(self._health_check_loop())

    async def _health_check_loop(self):
        """Background health check loop"""
        while self.stats.connected:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                await self._check_connection_health()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error for tenant {self.tenant_id}: {e}")

    async def _check_connection_health(self):
        """Check if ARI connection is still healthy"""
        if not self.client:
            return
            
        try:
            # Simple health check operation
            await self.execute_operation(self.client.test_connection)
        except Exception as e:
            logger.warning(f"ARI health check failed for tenant {self.tenant_id}: {e}")
            self.stats.connected = False
            await self._schedule_reconnect()

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


class ARIManager:
    """
    Global ARI Manager for handling multiple tenant connections
    Provides connection pooling, health monitoring, and resource management
    """
    
    def __init__(self):
        self.connections: Dict[str, ARIConnection] = {}
        self._lock = asyncio.Lock()
        logger.info("ARI Manager initialized")

    async def get_connection(self, tenant_id: str) -> Optional[ARIConnection]:
        """Get or create ARI connection for a tenant"""
        async with self._lock:
            if tenant_id in self.connections:
                connection = self.connections[tenant_id]
                if connection.stats.connected:
                    return connection
                else:
                    # Try to reconnect existing connection
                    if await connection.connect():
                        return connection
                    else:
                        # Remove failed connection
                        del self.connections[tenant_id]
            
            # Create new connection
            try:
                config = await self._get_tenant_ari_config(tenant_id)
                if not config:
                    logger.error(f"No ARI configuration found for tenant {tenant_id}")
                    return None
                    
                connection = ARIConnection(tenant_id, config)
                if await connection.connect():
                    self.connections[tenant_id] = connection
                    return connection
                else:
                    logger.error(f"Failed to establish ARI connection for tenant {tenant_id}")
                    return None
                    
            except Exception as e:
                logger.error(f"Error creating ARI connection for tenant {tenant_id}: {e}")
                return None

    async def disconnect_tenant(self, tenant_id: str):
        """Disconnect ARI for a specific tenant"""
        async with self._lock:
            if tenant_id in self.connections:
                await self.connections[tenant_id].disconnect()
                del self.connections[tenant_id]
                logger.info(f"ARI connection removed for tenant {tenant_id}")

    async def disconnect_all(self):
        """Disconnect all ARI connections"""
        async with self._lock:
            for connection in list(self.connections.values()):
                await connection.disconnect()
            self.connections.clear()
            logger.info("All ARI connections disconnected")

    async def execute_operation(self, tenant_id: str, operation_func: Callable, *args, **kwargs):
        """Execute ARI operation for a specific tenant"""
        connection = await self.get_connection(tenant_id)
        if connection:
            return await connection.execute_operation(operation_func, *args, **kwargs)
        return None

    def register_event_handler(self, tenant_id: str, event_type: str, handler: Callable):
        """Register event handler for a tenant"""
        if tenant_id in self.connections:
            self.connections[tenant_id].register_event_handler(event_type, handler)

    def get_connection_stats(self) -> Dict[str, ARIConnectionStats]:
        """Get connection statistics for all tenants"""
        return {tenant_id: conn.stats for tenant_id, conn in self.connections.items()}

    async def health_check(self) -> Dict[str, bool]:
        """Check health of all ARI connections"""
        health_status = {}
        for tenant_id, connection in self.connections.items():
            try:
                await connection._check_connection_health()
                health_status[tenant_id] = connection.stats.connected
            except Exception as e:
                logger.error(f"Health check failed for tenant {tenant_id}: {e}")
                health_status[tenant_id] = False
        return health_status

    async def _get_tenant_ari_config(self, tenant_id: str) -> Optional[ARIConfig]:
        """Get ARI configuration for a specific tenant"""
        try:
            # Get tenant
            tenant = await asyncio.get_event_loop().run_in_executor(
                None, lambda: Tenant.objects.get(tenant_id=tenant_id)
            )
            
            # Get ARI configuration from SystemConfiguration
            ari_host = await self._get_config_value(tenant_id, 'ari_host', 'localhost')
            ari_port = await self._get_config_value(tenant_id, 'ari_port', 8088)
            ari_username = await self._get_config_value(tenant_id, 'ari_username', 'asterisk')
            ari_password = await self._get_config_value(tenant_id, 'ari_password', 'asterisk')
            ari_app_name = await self._get_config_value(tenant_id, 'ari_app_name', f'aiMediaGateway_{tenant_id}')
            ari_timeout = await self._get_config_value(tenant_id, 'ari_timeout', 30)
            ari_max_retries = await self._get_config_value(tenant_id, 'ari_max_retries', 3)
            ari_retry_delay = await self._get_config_value(tenant_id, 'ari_retry_delay', 5)
            
            return ARIConfig(
                host=ari_host,
                port=int(ari_port),
                username=ari_username,
                password=ari_password,
                app_name=ari_app_name,
                timeout=int(ari_timeout),
                max_retries=int(ari_max_retries),
                retry_delay=int(ari_retry_delay)
            )
            
        except Exception as e:
            logger.error(f"Error getting ARI config for tenant {tenant_id}: {e}")
            return None

    @staticmethod
    async def _get_config_value(tenant_id: str, config_key: str, default_value):
        """Get configuration value for a tenant"""
        try:
            config = await asyncio.get_event_loop().run_in_executor(
                None, lambda: SystemConfiguration.objects.get(
                    tenant_id=tenant_id,
                    config_key=config_key
                )
            )
            return config.get_typed_value()
        except SystemConfiguration.DoesNotExist:
            logger.debug(f"Config {config_key} not found for tenant {tenant_id}, using default: {default_value}")
            return default_value
        except Exception as e:
            logger.error(f"Error getting config {config_key} for tenant {tenant_id}: {e}")
            return default_value


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
            asyncio.create_task(_ari_manager.disconnect_all())
        except RuntimeError:
            # No event loop running, cleanup without disconnection
            logger.debug("No event loop running, cleaning up ARI manager without disconnection")
        _ari_manager = None
