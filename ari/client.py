"""Main ARI client implementation.

This module provides the primary ARIClient class for interacting with Asterisk's
REST Interface (ARI) using an async-first approach.
"""

import asyncio
import json
import logging
import random
from typing import Optional, Dict, Any, List, Union, AsyncGenerator, Callable, Coroutine
from types import TracebackType
import aiohttp
from aiohttp import BasicAuth, ClientTimeout, TCPConnector
from ari.resources import BridgeResource, ChannelResource
from ari.config import ARIConfig
from ari.exceptions import (
    ARIError,
    AuthenticationError,
    ConnectionError,
    HTTPError,
    ResourceNotFoundError,
    ValidationError,
    WebSocketError,
    parse_asterisk_error,
)

from ari.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

# Set up logging
logger = logging.getLogger(__name__)


class ARIClient:
    """Async-first client for Asterisk REST Interface (ARI).

    This class provides the main interface for interacting with Asterisk's ARI.
    It supports both REST API calls and WebSocket event subscriptions with
    automatic connection management, error handling, and retry logic.

    Attributes:
        config: Configuration object with connection settings
        session: aiohttp ClientSession for HTTP requests
        websocket: Active WebSocket connection (if connected)
        is_connected: Whether the client is currently connected
        available_endpoints: Dictionary of discovered ARI endpoints

    Example:
        ```python
        import asyncio
        from asterisk_ari import ARIClient, ARIConfig

        async def main():
            config = ARIConfig(
                host="localhost",
                username="asterisk",
                password="asterisk",
                app_name="hello_world"
            )

            async with ARIClient(config) as client:
                channels = await client.get_channels()
                print(f"Active channels: {len(channels)}")

        asyncio.run(main())
        ```
    """

    def __init__(self, config: ARIConfig) -> None:
        """Initialize ARI client.

        Args:
            config: Configuration object with connection settings
        """
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connector: Optional[TCPConnector] = None
        self._is_connected = False
        self._available_endpoints: Dict[str, Any] = {}
        self._event_handlers: Dict[str, List[Callable]] = {}
        self._websocket_task: Optional[asyncio.Task] = None
        self._should_reconnect = True
        self._websocket_ping_task: Optional[asyncio.Task] = None
        self._last_pong_time: Optional[float] = None
        self._event_buffer: List[Dict[str, Any]] = []
        self._websocket_connection_time: Optional[float] = None

        # Initialize circuit breaker for preventing cascade failures
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=config.circuit_breaker_failure_threshold,
            recovery_timeout=config.circuit_breaker_recovery_timeout,
            expected_exception=ConnectionError,
            name=f"ARI-{config.host}:{config.port}"
        )

        # Graceful degradation flags for optional features
        self._endpoint_discovery_failed = False
        self._websocket_available = True
        self._optional_features_status = {
            'endpoint_discovery': True,
            'websocket_events': True,
            'bridge_operations': True,
            'playback_operations': True,
            'recording_operations': True,
        }

        # Set up logging level from config
        if config.debug:
            logging.getLogger("asterisk_ari").setLevel(logging.DEBUG)
            logging.getLogger("aiohttp").setLevel(logging.DEBUG)
        else:
            logging.getLogger("asterisk_ari").setLevel(getattr(logging, config.log_level))

    async def __aenter__(self) -> "ARIClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
            self,
            exc_type: Optional[type],
            exc_val: Optional[Exception],
            exc_tb: Optional[TracebackType],
    ) -> None:
        """Async context manager exit."""
        await self.disconnect()

    @property
    def session(self) -> aiohttp.ClientSession:
        """Get the HTTP session, creating it if necessary.

        Returns:
            aiohttp ClientSession instance

        Raises:
            ConnectionError: If session creation fails
        """
        if self._session is None or self._session.closed:
            raise ConnectionError("Client is not connected. Use async context manager or call connect() first.")
        return self._session

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected to Asterisk.

        Returns:
            True if connected, False otherwise
        """
        return (
                self._is_connected
                and self._session is not None
                and not self._session.closed
        )

    @property
    def websocket(self) -> Optional[aiohttp.ClientWebSocketResponse]:
        """Get the active WebSocket connection.

        Returns:
            WebSocket connection if active, None otherwise
        """
        return self._websocket

    @property
    def available_endpoints(self) -> Dict[str, Any]:
        """Get the discovered ARI endpoints.

        Returns:
            Dictionary of available endpoints from Asterisk
        """
        return self._available_endpoints.copy()

    @property
    def feature_status(self) -> Dict[str, bool]:
        """Get the status of optional features.

        Returns:
            Dictionary mapping feature names to their availability status
        """
        return self._optional_features_status.copy()

    def is_feature_available(self, feature_name: str) -> bool:
        """Check if a specific optional feature is available.

        Args:
            feature_name: Name of the feature to check

        Returns:
            True if feature is available, False otherwise
        """
        return self._optional_features_status.get(feature_name, False)

    async def safe_request(
            self,
            method: str,
            endpoint: str,
            feature_name: Optional[str] = None,
            fallback_result: Any = None,
            **kwargs: Any
    ) -> Any:
        """Make a safe request with graceful degradation.

        This method attempts to make a request and provides fallback behavior
        if the associated optional feature is not available.

        Args:
            method: HTTP method
            endpoint: API endpoint
            feature_name: Associated feature name for availability check
            fallback_result: Result to return if feature is unavailable
            **kwargs: Additional request arguments

        Returns:
            Response data or fallback result
        """
        # Check feature availability if specified
        if feature_name and not self.is_feature_available(feature_name):
            logger.warning(
                f"Feature '{feature_name}' is not available, "
                f"returning fallback result for {method} {endpoint}"
            )
            return fallback_result

        try:
            return await self._request(method, endpoint, **kwargs)
        except (HTTPError, ResourceNotFoundError) as e:
            if feature_name:
                logger.warning(
                    f"Optional feature '{feature_name}' failed: {e}, "
                    f"returning fallback result"
                )
                # Mark feature as unavailable for future requests
                self._optional_features_status[feature_name] = False
                return fallback_result
            else:
                # Re-raise for core functionality
                raise

    async def connect(self) -> None:
        """Establish connection to Asterisk ARI.

        This method creates the HTTP session, discovers available endpoints,
        and verifies authentication.

        Raises:
            ConnectionError: If connection to Asterisk fails
            AuthenticationError: If authentication fails
        """
        if self.is_connected:
            logger.debug("Client already connected")
            return

        logger.info(f"Connecting to Asterisk ARI at {self.config.ari_url}")

        try:
            # Create TCP connector
            connector_kwargs = self.config.get_connector_kwargs()

            self._connector = TCPConnector(**connector_kwargs)

            # Create timeout configuration
            timeout_config = self.config.get_timeout_config()
            timeout = ClientTimeout(**timeout_config)

            # Create HTTP session
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=timeout,
                auth=BasicAuth(*self.config.auth_tuple),
                headers={"User-Agent": self.config.user_agent},
                json_serialize=json.dumps,
            )

            # Test connection and authentication
            await self._test_connection()

            # Discover available endpoints
            await self._discover_endpoints()

            self._is_connected = True
            logger.info("Successfully connected to Asterisk ARI")

        except Exception as e:
            await self._cleanup_connection()
            if isinstance(e, (AuthenticationError, ConnectionError)):
                raise
            else:
                raise ConnectionError(f"Failed to connect to Asterisk: {e}") from e

    async def close(self) -> None:
        await self.disconnect()

    async def disconnect(self) -> None:
        """Disconnect from Asterisk ARI.

        This method closes the WebSocket connection (if active) and HTTP session,
        and performs cleanup of resources.
        """
        if not self.is_connected:
            logger.debug("Client not connected")
            return

        logger.info("Disconnecting from Asterisk ARI")

        # Set flag to prevent reconnection
        self._should_reconnect = False

        # Close WebSocket connection
        if self._websocket_task and not self._websocket_task.done():
            self._websocket_task.cancel()
            try:
                await self._websocket_task
            except asyncio.CancelledError:
                pass

        await self._close_websocket()
        await self._cleanup_connection()

        self._is_connected = False
        logger.info("Disconnected from Asterisk ARI")

    @property
    def test_connection(self):
        return self._test_connection()

    async def _test_connection(self) -> None:
        """Test connection and authentication with Asterisk.

        Raises:
            AuthenticationError: If authentication fails
            ConnectionError: If connection fails
        """
        try:
            url = f"{self.config.ari_url}/asterisk/info"
            response = await self.session.get(url, timeout=self.config.timeout)
            async with response:
                if response.status == 401:
                    raise AuthenticationError(
                        "Authentication failed - invalid username or password",
                        username=self.config.username,
                        status_code=response.status,
                    )
                elif response.status == 404:
                    raise ConnectionError(
                        f"ARI endpoint not found at {url}. Ensure ARI is enabled in Asterisk.",
                        host=self.config.host,
                        port=self.config.port,
                    )
                elif not response.ok:
                    error_text = await response.text()
                    raise ConnectionError(
                        f"Connection test failed: HTTP {response.status} - {error_text}",
                        host=self.config.host,
                        port=self.config.port,
                    )

                # Parse response to verify it's valid
                try:
                    info = await response.json()
                    logger.debug(f"Connected to Asterisk {info.get('version', 'unknown')}")
                except json.JSONDecodeError as e:
                    raise ConnectionError(
                        f"Invalid response from Asterisk: {e}",
                        host=self.config.host,
                        port=self.config.port,
                    ) from e

        except aiohttp.ClientError as e:
            raise ConnectionError(
                f"Network error connecting to Asterisk: {e}",
                host=self.config.host,
                port=self.config.port,
                timeout=self.config.timeout,
            ) from e

    async def _discover_endpoints(self) -> None:
        """Discover available ARI endpoints from Asterisk with graceful degradation.

        This method fetches the OpenAPI specification from Asterisk and
        stores information about available endpoints for dynamic method generation.
        If endpoint discovery fails, core functionality continues to work.
        """
        try:
            url = f"{self.config.ari_url}/api-docs/resources.json"
            async with self.session.get(url) as response:
                if response.ok:
                    self._available_endpoints = await response.json()
                    apis = self._available_endpoints.get('apis', [])
                    logger.debug(f"Discovered {len(apis)} API categories")

                    # Update feature availability based on discovered endpoints
                    self._update_feature_availability(apis)
                    self._endpoint_discovery_failed = False
                    self._optional_features_status['endpoint_discovery'] = True
                else:
                    logger.warning(f"Failed to discover endpoints: HTTP {response.status}")
                    self._handle_endpoint_discovery_failure()

        except Exception as e:
            logger.warning(f"Endpoint discovery failed: {e}")
            self._handle_endpoint_discovery_failure()

    def _update_feature_availability(self, apis: List[Dict[str, Any]]) -> None:
        """Update feature availability based on discovered endpoints."""
        available_paths = set()
        for api in apis:
            for operation in api.get('operations', []):
                available_paths.add(operation.get('path', ''))

        # Check for specific feature availability
        self._optional_features_status['bridge_operations'] = any(
            '/bridges' in path for path in available_paths
        )
        self._optional_features_status['playback_operations'] = any(
            '/playbacks' in path for path in available_paths
        )
        self._optional_features_status['recording_operations'] = any(
            '/recordings' in path for path in available_paths
        )

        logger.debug(f"Feature availability: {self._optional_features_status}")

    def _handle_endpoint_discovery_failure(self) -> None:
        """Handle endpoint discovery failure with graceful degradation."""
        self._available_endpoints = {}
        self._endpoint_discovery_failed = True
        self._optional_features_status['endpoint_discovery'] = False

        # Assume basic features are available for graceful degradation
        self._optional_features_status.update({
            'bridge_operations': True,  # Assume available for fallback
            'playback_operations': True,
            'recording_operations': True,
        })

        logger.info(
            "Endpoint discovery failed - continuing with assumed feature availability. "
            "Some advanced features may not work correctly."
        )

    async def _cleanup_connection(self) -> None:
        """Clean up connection resources."""
        if self._session and not self._session.closed:
            await self._session.close()

        if self._connector:
            await self._connector.close()

        self._session = None
        self._connector = None

    async def _close_websocket(self) -> None:
        """Close the WebSocket connection."""
        if self._websocket and not self._websocket.closed:
            await self._websocket.close()
        self._websocket = None
        self._websocket_available = False

    # WebSocket Event Methods

    async def connect_websocket(self) -> None:
        """Establish WebSocket connection for real-time events.

        This method connects to the ARI WebSocket endpoint for receiving
        real-time events from Asterisk.

        Raises:
            ConnectionError: If WebSocket connection fails
            AuthenticationError: If authentication fails
        """
        if not self.is_connected:
            raise ConnectionError("HTTP client must be connected before WebSocket")

        try:
            ws_url = f"{self.config.websocket_url}?app={self.config.app_name}"
            logger.info(f"Connecting to WebSocket at {ws_url}")

            # Create WebSocket connection with authentication
            self._websocket = await self.session.ws_connect(
                ws_url,
                auth=BasicAuth(*self.config.auth_tuple),
                **self.config.get_websocket_kwargs()
            )

            self._websocket_available = True
            self._optional_features_status['websocket_events'] = True
            self._websocket_connection_time = asyncio.get_event_loop().time()
            self._last_pong_time = self._websocket_connection_time
            logger.info("WebSocket connection established")

            # Start event processing task
            if self._websocket_task is None or self._websocket_task.done():
                self._websocket_task = asyncio.create_task(self._websocket_event_loop())

            # Start ping/pong heartbeat task
            # if self._websocket_ping_task is None or self._websocket_ping_task.done():
            #     self._websocket_ping_task = asyncio.create_task(self._websocket_ping_loop())

            # Process buffered events if any
            await self._process_buffered_events()

        except Exception as e:
            self._websocket_available = False
            self._optional_features_status['websocket_events'] = False
            logger.error(f"WebSocket connection failed: {e}")
            raise ConnectionError(f"Failed to connect WebSocket: {e}") from e

    async def disconnect_websocket(self) -> None:
        """Disconnect the WebSocket connection."""
        self._should_reconnect = False

        if self._websocket_task and not self._websocket_task.done():
            self._websocket_task.cancel()
            try:
                await self._websocket_task
            except asyncio.CancelledError:
                pass

        if self._websocket_ping_task and not self._websocket_ping_task.done():
            self._websocket_ping_task.cancel()
            try:
                await self._websocket_ping_task
            except asyncio.CancelledError:
                pass

        await self._close_websocket()
        logger.info("WebSocket disconnected")

    async def _websocket_event_loop(self) -> None:
        """Main WebSocket event processing loop.

        This method handles incoming WebSocket messages and dispatches
        events to registered handlers.
        """
        try:
            async for msg in self._websocket:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_websocket_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.PONG:
                    self._last_pong_time = asyncio.get_event_loop().time()
                    logger.debug("Received WebSocket pong")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self._websocket.exception()}")
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    logger.info("WebSocket connection closed")
                    break

        except asyncio.CancelledError:
            logger.debug("WebSocket event loop cancelled")
            raise
        except Exception as e:
            logger.error(f"WebSocket event loop error: {e}")
        finally:
            if self._should_reconnect and self._websocket_available:
                logger.info("WebSocket disconnected, attempting reconnection")
                await self._attempt_websocket_reconnection()

    async def _websocket_ping_loop(self) -> None:
        """Ping/pong heartbeat loop for WebSocket connection health monitoring."""
        try:
            while self._websocket and not self._websocket.closed:
                await asyncio.sleep(self.config.websocket_ping_interval)

                if self._websocket and not self._websocket.closed:
                    try:
                        # Send ping
                        await self._websocket.ping()
                        logger.info("--- Sent WebSocket Ping ---")

                        # Check if we received pong within timeout
                        await asyncio.sleep(1.0)  # Small delay to allow pong response
                        current_time = asyncio.get_event_loop().time()

                        if (self._last_pong_time and
                                (current_time - self._last_pong_time) > self.config.websocket_pong_timeout):
                            logger.warning("WebSocket pong timeout - connection may be unhealthy")
                            # Don't break here, let the main loop handle reconnection

                    except Exception as e:
                        logger.error(f"WebSocket ping failed: {e}")
                        break

        except asyncio.CancelledError:
            logger.debug("WebSocket ping loop cancelled")
            raise
        except Exception as e:
            logger.error(f"WebSocket ping loop error: {e}")

    async def _process_buffered_events(self) -> None:
        """Process events that were buffered during WebSocket disconnection."""
        if not self._event_buffer:
            return

        logger.info(f"Processing {len(self._event_buffer)} buffered events")

        for event in self._event_buffer:
            try:
                event_type = event.get('type', 'Unknown')
                await self._dispatch_event(event_type, event)
            except Exception as e:
                logger.error(f"Error processing buffered event: {e}")

        # Clear the buffer after processing
        self._event_buffer.clear()
        logger.debug("Buffered events processed and cleared")

    async def _handle_websocket_message(self, message_data: str) -> None:
        """Handle incoming WebSocket message.

        Args:
            message_data: Raw WebSocket message data
        """
        try:
            event = json.loads(message_data)
            event_type = event.get('type', 'Unknown')

            logger.debug(f"Received WebSocket event: {event_type}")

            # Dispatch event to handlers
            await self._dispatch_event(event_type, event)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse WebSocket message: {e}")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")

    async def _dispatch_event(self, event_type: str, event_data: Dict[str, Any]) -> None:
        """Dispatch event to registered handlers.

        Args:
            event_type: Type of the event
            event_data: Event data dictionary
        """
        # If WebSocket is not available, buffer the event
        if not self._websocket_available:
            self._event_buffer.append(event_data)
            logger.debug(f"Buffered event {event_type} (buffer size: {len(self._event_buffer)})")
            return

        # Dispatch to specific event handlers
        if event_type in self._event_handlers:
            for handler in self._event_handlers[event_type]:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event_data)
                    else:
                        handler(event_data)
                except Exception as e:
                    logger.error(f"Error in event handler for {event_type}: {e}")
                    logger.error(f"Handler function: {handler}")
                    logger.error(f"Event data: {event_data}")

        # Dispatch to wildcard handlers
        if '*' in self._event_handlers:
            for handler in self._event_handlers['*']:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event_data)
                    else:
                        handler(event_data)
                except Exception as e:
                    logger.error(f"Error in wildcard event handler: {e}")

    async def _attempt_websocket_reconnection(self) -> None:
        """Attempt to reconnect WebSocket with exponential backoff."""
        max_attempts = self.config.retry_attempts

        for attempt in range(max_attempts):
            try:
                delay = self._calculate_retry_delay(attempt)
                logger.info(f"Attempting WebSocket reconnection in {delay:.2f}s (attempt {attempt + 1})")
                await asyncio.sleep(delay)

                await self.connect_websocket()
                logger.info("WebSocket reconnection successful")
                return

            except Exception as e:
                logger.warning(f"WebSocket reconnection attempt {attempt + 1} failed: {e}")
                if attempt == max_attempts - 1:
                    logger.error("WebSocket reconnection failed after maximum attempts")
                    self._websocket_available = False
                    self._optional_features_status['websocket_events'] = False

    def on_event(self, event_type: str, handler: Callable) -> None:
        """Register an event handler for specific event types.

        Args:
            event_type: Event type to listen for (use '*' for all events)
            handler: Callback function to handle the event
        """
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []

        self._event_handlers[event_type].append(handler)
        logger.debug(f"Registered handler for event type: {event_type}")

    def remove_event_handler(self, event_type: str, handler: Callable) -> bool:
        """Remove an event handler.

        Args:
            event_type: Event type
            handler: Handler function to remove

        Returns:
            True if handler was removed, False if not found
        """
        if event_type in self._event_handlers:
            try:
                self._event_handlers[event_type].remove(handler)
                if not self._event_handlers[event_type]:
                    del self._event_handlers[event_type]
                logger.debug(f"Removed handler for event type: {event_type}")
                return True
            except ValueError:
                pass
        return False

    def get_websocket_health(self) -> Dict[str, Any]:
        """Get WebSocket connection health status.

        Returns:
            Dictionary containing WebSocket health information
        """
        current_time = asyncio.get_event_loop().time()

        return {
            'connected': self._websocket_available,
            'connection_time': self._websocket_connection_time,
            'last_pong_time': self._last_pong_time,
            'uptime_seconds': (
                current_time - self._websocket_connection_time
                if self._websocket_connection_time else 0
            ),
            'last_ping_latency': (
                current_time - self._last_pong_time
                if self._last_pong_time else None
            ),
            'buffered_events_count': len(self._event_buffer),
            'ping_interval': self.config.websocket_ping_interval,
            'pong_timeout': self.config.websocket_pong_timeout,
        }

    def is_websocket_healthy(self) -> bool:
        """Check if WebSocket connection is healthy.

        Returns:
            True if WebSocket is healthy, False otherwise
        """
        if not self._websocket_available:
            return False

        if not self._last_pong_time:
            return True  # Just connected, no pings sent yet

        current_time = asyncio.get_event_loop().time()
        time_since_pong = current_time - self._last_pong_time

        # Consider unhealthy if no pong received within 2x the ping interval + pong timeout
        max_allowed_silence = (self.config.websocket_ping_interval * 2) + self.config.websocket_pong_timeout

        return time_since_pong <= max_allowed_silence

    def get_connection_status(self) -> Dict[str, Any]:
        """Get comprehensive connection status.

        Returns:
            Dictionary containing complete connection status information
        """
        return {
            'http_connected': self.is_connected,
            'websocket_connected': self._websocket_available,
            'websocket_healthy': self.is_websocket_healthy(),
            'circuit_breaker_state': str(self._circuit_breaker.state),
            'circuit_breaker_stats': {
                'failure_count': self._circuit_breaker.stats.failure_count,
                'success_count': self._circuit_breaker.stats.success_count,
                'last_failure_time': self._circuit_breaker.stats.last_failure_time,
                'last_success_time': self._circuit_breaker.stats.last_success_time,
            },
            'feature_status': self.feature_status,
            'websocket_health': self.get_websocket_health(),
            'endpoint_discovery_failed': self._endpoint_discovery_failed,
            'available_endpoints_count': len(self._available_endpoints),
        }

    # HTTP Request Methods

    async def _request(
            self,
            method: str,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            json_data: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        """Make an authenticated HTTP request to the ARI API with retry logic and circuit breaker.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path (e.g., '/channels')
            params: Query parameters
            json_data: JSON data for request body
            **kwargs: Additional arguments for aiohttp request

        Returns:
            Parsed JSON response data

        Raises:
            AuthenticationError: If authentication fails
            HTTPError: If HTTP request fails
            ConnectionError: If connection fails
            CircuitBreakerOpenError: If circuit breaker is open
        """
        if not self.is_connected:
            raise ConnectionError("Client not connected")

        # Use circuit breaker to protect against cascade failures
        return await self._circuit_breaker.call(
            self._request_with_retry, method, endpoint, params, json_data, **kwargs
        )

    async def _request_with_retry(
            self,
            method: str,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            json_data: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        """Internal method to make HTTP request with retry logic.

        This method is called by the circuit breaker and contains the actual
        retry logic with exponential backoff.
        """
        url = f"{self.config.ari_url}{endpoint}"
        last_exception = None

        # Retry logic with exponential backoff
        for attempt in range(self.config.retry_attempts + 1):
            try:
                # logger.debug(f"{method} {url} (attempt {attempt + 1}/{self.config.retry_attempts + 1})")

                async with self.session.request(
                        method,
                        url,
                        params=params,
                        json=json_data,
                        **kwargs
                ) as response:

                    # Handle authentication errors (don't retry)
                    if response.status == 401:
                        raise AuthenticationError(
                            "Authentication failed",
                            username=self.config.username,
                            status_code=response.status,
                        )

                    # Handle client errors (4xx - don't retry except 429)
                    if 400 <= response.status < 500 and response.status != 429:
                        try:
                            error_data = await response.json()
                        except json.JSONDecodeError:
                            error_data = await response.text()

                        raise parse_asterisk_error(
                            error_data,
                            response.status,
                            method,
                            url,
                        )

                    # Handle successful responses
                    if response.ok:
                        if response.content_type == "application/json":
                            return await response.json()
                        else:
                            return await response.text()

                    # Handle server errors (5xx) and rate limiting (429) - retry these
                    if response.status >= 500 or response.status == 429:
                        try:
                            error_data = await response.json()
                        except json.JSONDecodeError:
                            error_data = await response.text()

                        last_exception = parse_asterisk_error(
                            error_data,
                            response.status,
                            method,
                            url,
                        )

                        if attempt < self.config.retry_attempts:
                            delay = self._calculate_retry_delay(attempt)
                            logger.warning(
                                f"Request failed with status {response.status}, "
                                f"retrying in {delay:.2f}s (attempt {attempt + 1})"
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            raise last_exception

            except aiohttp.ClientError as e:
                last_exception = ConnectionError(
                    f"Network error during {method} {url}: {e}",
                    host=self.config.host,
                    port=self.config.port,
                )

                if attempt < self.config.retry_attempts:
                    delay = self._calculate_retry_delay(attempt)
                    logger.warning(
                        f"Network error occurred, retrying in {delay:.2f}s "
                        f"(attempt {attempt + 1}): {e}"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    raise last_exception from e

        # This should not be reached, but just in case
        if last_exception:
            raise last_exception
        else:
            raise ConnectionError("Unexpected error in retry logic")

    def _calculate_retry_delay(self, attempt: int) -> float:
        """Calculate retry delay using exponential backoff with jitter.

        Args:
            attempt: Current attempt number (0-based)

        Returns:
            Delay in seconds
        """
        # Exponential backoff: base_delay * (backoff_factor ^ attempt)
        base_delay = self.config.retry_backoff
        delay = base_delay * (2 ** attempt)

        # Apply maximum delay limit
        delay = min(delay, self.config.max_retry_delay)

        # Add jitter to prevent thundering herd (±25% variation)
        jitter = delay * 0.25 * (2 * random.random() - 1)
        delay = max(0.1, delay + jitter)  # Ensure minimum delay of 0.1s

        return delay

    async def get(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        """Make a GET request to the ARI API.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            **kwargs: Additional request arguments

        Returns:
            Response data
        """
        return await self._request("GET", endpoint, params=params, **kwargs)

    async def post(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            json_data: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        """Make a POST request to the ARI API.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON data for request body
            **kwargs: Additional request arguments

        Returns:
            Response data
        """
        return await self._request("POST", endpoint, params=params, json_data=json_data, **kwargs)

    async def put(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            json_data: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        """Make a PUT request to the ARI API.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            json_data: JSON data for request body
            **kwargs: Additional request arguments

        Returns:
            Response data
        """
        return await self._request("PUT", endpoint, params=params, json_data=json_data, **kwargs)

    async def delete(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        """Make a DELETE request to the ARI API.

        Args:
            endpoint: API endpoint path
            params: Query parameters
            **kwargs: Additional request arguments

        Returns:
            Response data
        """
        return await self._request("DELETE", endpoint, params=params, **kwargs)

    # Basic ARI Operations

    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get all active channels.

        Returns:
            List of channel data dictionaries

        Example:
            ```python
            async with ARIClient(config) as client:
                channels = await client.get_channels()
                for channel in channels:
                    print(f"Channel: {channel['id']} - {channel['name']}")
            ```
        """
        return await self.get("/channels")

    async def get_channel(self, channel_id: str) -> Dict[str, Any]:
        """Get details for a specific channel.

        Args:
            channel_id: Channel ID

        Returns:
            Channel data dictionary

        Raises:
            ResourceNotFoundError: If channel not found
        """
        return await self.get(f"/channels/{channel_id}")


    async def create_channel(
            self,
            endpoint: str,
            extension: Optional[str] = None,
            context: Optional[str] = None,
            priority: Optional[int] = None,
            label: Optional[str] = None,
            app: Optional[str] = None,
            app_args: Optional[str] = None,
            callerId: Optional[str] = None,
            timeout: Optional[int] = 30,
            channel_id: Optional[str] = None,
            other_channel_id: Optional[str] = None,
            originator: Optional[str] = None,
            formats: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Asynchronously creates a channel with the specified settings and parameters. This function utilizes an HTTP POST
        request to initiate a channel creation operation with the given configurations.

        :param endpoint: The endpoint to use for the channel. Required.
        :param extension: The extension associated with the channel. Optional.
        :param context: The dialplan context for the channel. Optional.
        :param priority: The priority of the channel in the dialplan. Optional.
        :param label: The label to set for the channel. Optional.
        :param app: The application to execute on the created channel. Optional.
        :param app_args: The arguments to pass to the application on the created channel. Optional.
        :param callerId: The Caller ID to use when creating the channel. Optional.
        :param timeout: The time in seconds before the request times out. Optional.
        :param channel_id: Specific ID to assign to the channel being created. Optional.
        :param other_channel_id: ID of another channel to associate with during creation. Optional.
        :param originator: The originator of the channel creation request, if applicable. Optional.
        :param formats: The format(s) to use for the media in the channel. Optional.
        :return: A dictionary containing the response data from the channel creation request.
        :rtype: Dict[str, Any]
        """
        params = {
            "endpoint": endpoint,
            "app": app or self.config.app_name,
        }

        if extension:
            params["extension"] = extension
        if context:
            params["context"] = context
        if priority:
            params["priority"] = priority
        if label:
            params["label"] = label
        if callerId:
            params["callerId"] = callerId
        if timeout:
            params["timeout"] = timeout

        if app_args:
            params["appArgs"] = app_args
        if channel_id:
            params["channelId"] = channel_id
        if other_channel_id:
            params["otherChannelId"] = other_channel_id
        if originator:
            params["originator"] = originator
        if formats:
            params["formats"] = formats

        return await self.post("/channels", params=params)

    async def continue_to_dialplan(self, channel_id: str) -> Dict[str, Any]:
        """Continue to dialplan for a specific channel."""
        return await self.post(f"/channels/{channel_id}/continue")

    async def create_external_media(
            self,
            external_host: str,
            app: Optional[str] = None,
            codec: Optional[str] = 'slin16',
            encapsulation: Optional[str] = 'rtp',
            transport: Optional[str] = 'udp',
            direction: Optional[str] = 'both',
            connection_type: Optional[str] = 'client',
            channel_id: Optional[str] = None,
    ) -> ChannelResource:
        """
        Creates an external media channel by interacting with an external system using
        HTTP POST. This method allows specifying various parameters to configure the
        external media connection such as codec, transport method, direction, and more.

        Detailed customization of the media connection can be done via arguments
        such as app name, transport type, media direction, and external host details.
        Additional optional parameters for advanced configurations like originating
        media status, associated channel ID, or additional variables for the connection
        can be provided.

        :param app: The name of the application creating the external media connection.
        :param external_host: The external host address in the form of host:port.
        :param codec: The codec to be used for the external media; default is 'slin16'.
        :param encapsulation: The encapsulation type to be used for the external media;
        :param transport: The transport protocol to be used; default is 'udp'.
        :param direction: The direction of media flow; 'send', 'receive', or 'both'.
        :param connection_type: The type of connection setup, either 'client' or 'server'.
        :param channel_id: The unique identifier of the channel to be used, if applicable.
        :return: A dictionary containing the result of the external media channel creation.
        """
        params = {
            "app": app or self.config.app_name,
            "external_host": external_host or f"{self.config.external_media_host}:{self.config.external_media_port}",
            "format": codec,
            "transport": transport,
            "direction": direction,
            "connection_type": connection_type,
            "encapsulation": encapsulation,
        }

        if channel_id:
            params["channelId"] = channel_id

        external = await self.post("/channels/externalMedia", params=params)
        return ChannelResource(self, external)

    async def snoop_channel(
            self,
            channel_id: str,
            spy: Optional[str] = None,
            whisper: Optional[str] = None,
            app: Optional[str] = None,
            appArgs: Optional[str] = None,
            snoopId: Optional[str] = None
    ) -> ChannelResource:
        """
        Snoops a channel by creating a specified snoop resource using the provided
        parameters. This method enables an application to configure monitoring
        or interaction functionalities on a particular communication channel.

        :param channel_id: The unique identifier of the channel to snoop.
        :param spy: Spy mode configuration (optional). If set, enables a specific
            spy mode for handling the snooping session.
        :param whisper: Whisper mode configuration (optional). If set, enables
            a specific whisper mode while snooping the channel.
        :param app: Application name override (optional). If provided, overrides
            the default application name for the snoop configuration.
        :param appArgs: A list of additional arguments (optional). These provide
            specific configurations or instructions for the application associated
            with the snoop session.
        :param snoopId: A unique identifier for the snoop session (optional).
            If provided, it will associate this identifier with the snooping operation.
        :return: A `ChannelResource` instance that encapsulates the details of
            the created snoop resource on the channel.
        """
        params = {
            "app": app or self.config.app_name,
            "channelId": channel_id,
        }

        if spy:
            params["spy"] = spy

        if whisper:
            params["whisper"] = whisper
        if appArgs:
            params["appArgs"] = appArgs
        if snoopId:
            params["snoopId"] = snoopId

        snoop = await self.post(f"/channels/{channel_id}/snoop", params=params)
        return ChannelResource(self, snoop)

    async def answer_channel(self, channel_id: str) -> None:
        """Answer a channel.

        Args:
            channel_id: Channel ID to answer

        Example:
            ```python
            async with ARIClient(config) as client:
                await client.answer_channel("channel_id")
            ```
        """
        await self.post(f"/channels/{channel_id}/answer")

    async def hangup_channel(self, channel_id: str, reason: Optional[str] = None) -> None:
        """Hang up a channel.

        Args:
            channel_id: Channel ID to hang up
            reason: Hangup reason (optional)

        Example:
            ```python
            async with ARIClient(config) as client:
                await client.hangup_channel("channel_id", reason="normal")
            ```
        """
        params = {}
        if reason:
            params["reason"] = reason

        await self.delete(f"/channels/{channel_id}", params=params)

    async def get_bridges(self) -> List[Dict[str, Any]]:
        """Get all active bridges.

        Returns:
            List of bridge data dictionaries
        """
        return await self.get("/bridges")

    async def get_bridge(self, bridge_id: str) -> BridgeResource:
        """Get details for a specific bridge.

        Args:
            bridge_id: Bridge ID

        Returns:
            Bridge data dictionary

        Raises:
            ResourceNotFoundError: If bridge not found
        """
        bridge = await self.get(f"/bridges/{bridge_id}")
        return BridgeResource(self, bridge)

    async def create_bridge(
            self,
            bridge_type: str = "mixing",
            bridge_id: Optional[str] = None,
            name: Optional[str] = None,
    ) -> BridgeResource:
        """Create a new bridge.

        Args:
            bridge_type: Type of bridge (mixing, holding, dtmf_events, proxy_media)
            bridge_id: Bridge ID to use (optional)
            name: Bridge name (optional)

        Returns:
            Created bridge data dictionary
        """
        params = {"type": bridge_type}
        if bridge_id:
            params["bridgeId"] = bridge_id
        if name:
            params["name"] = name

        created_bridge = await self.post("/bridges", params=params)
        return BridgeResource(self, created_bridge)
