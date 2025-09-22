"""Pytest configuration and fixtures for asterisk-ari tests.

This module provides common fixtures and configuration for testing the
Asterisk ARI Python library.
"""

import pytest
import asyncio
from typing import Dict, Any, AsyncGenerator
from unittest.mock import AsyncMock, Mock, patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import make_mocked_coro

from ari import ARIConfig, ARIClient


@pytest.fixture
def ari_test_config() -> Dict[str, Any]:
    """Provide test configuration for ARI client.

    Returns:
        Dictionary with test configuration values
    """
    return {
        "host": "localhost",
        "port": 8088,
        "username": "test_user",
        "password": "test_password",
        "app_name": "test_app",
        "timeout": 30.0,
        "retry_attempts": 3,
        "debug": True,
    }


@pytest.fixture
def ari_config(ari_test_config: Dict[str, Any]) -> ARIConfig:
    """Provide ARIConfig instance for testing.

    Args:
        ari_test_config: Test configuration dictionary

    Returns:
        ARIConfig instance
    """
    return ARIConfig(**ari_test_config)


@pytest.fixture
def mock_channel_data() -> Dict[str, Any]:
    """Provide mock channel data for testing.

    Returns:
        Dictionary with mock channel data
    """
    return {
        "id": "channel_id_123",
        "name": "SIP/1000-00000001",
        "state": "Up",
        "caller": {
            "name": "Test Caller",
            "number": "1000"
        },
        "connected": {
            "name": "Test Connected",
            "number": "2000"
        },
        "accountcode": "",
        "dialplan": {
            "context": "default",
            "exten": "s",
            "priority": 1,
            "app_name": "test_app",
            "app_data": ""
        },
        "creationtime": "2023-09-21T12:00:00.000+0000",
        "language": "en"
    }


@pytest.fixture
def mock_bridge_data() -> Dict[str, Any]:
    """Provide mock bridge data for testing.

    Returns:
        Dictionary with mock bridge data
    """
    return {
        "id": "bridge_id_456",
        "technology": "simple_bridge",
        "bridge_type": "mixing",
        "bridge_class": "base",
        "name": "test_bridge",
        "channels": [],
        "creationtime": "2023-09-21T12:00:00.000+0000"
    }


@pytest.fixture
def mock_asterisk_info() -> Dict[str, Any]:
    """Provide mock Asterisk info for testing.

    Returns:
        Dictionary with mock Asterisk info
    """
    return {
        "version": "18.0.0",
        "build": {
            "os": "Linux",
            "kernel": "5.4.0",
            "options": "LOADABLE_MODULES, OPTIONAL_API"
        },
        "status": {
            "startup_time": "2023-09-21T10:00:00.000+0000",
            "last_reload_time": "2023-09-21T10:00:00.000+0000"
        }
    }


@pytest.fixture
def mock_aiohttp_session():
    """Provide mock aiohttp ClientSession for testing.

    Returns:
        Mock ClientSession with common methods
    """
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.closed = False

    # Mock response object
    response = AsyncMock()
    response.ok = True
    response.status = 200
    response.content_type = "application/json"
    response.json = AsyncMock(return_value={})
    response.text = AsyncMock(return_value="")

    # Configure session methods to return the response
    session.get.return_value.__aenter__.return_value = response
    session.post.return_value.__aenter__.return_value = response
    session.put.return_value.__aenter__.return_value = response
    session.delete.return_value.__aenter__.return_value = response
    session.request.return_value.__aenter__.return_value = response

    return session


@pytest.fixture
def mock_websocket():
    """Provide mock WebSocket connection for testing.

    Returns:
        Mock WebSocket connection
    """
    websocket = AsyncMock(spec=aiohttp.ClientWebSocketResponse)
    websocket.closed = False
    websocket.close = AsyncMock()

    return websocket


@pytest.fixture
async def mock_ari_server(aioloop, aiohttp_client):
    """Create mock ARI server for integration testing.

    Args:
        aioloop: Event loop fixture
        aiohttp_client: aiohttp test client fixture

    Returns:
        Mock ARI server instance
    """

    async def asterisk_info(request):
        """Mock asterisk info endpoint."""
        return web.json_response({
            "version": "18.0.0",
            "build": {"os": "Linux"},
            "status": {"startup_time": "2023-09-21T10:00:00.000+0000"}
        })

    async def api_docs(request):
        """Mock API docs endpoint."""
        return web.json_response({
            "swaggerVersion": "1.2",
            "apis": [
                {"path": "/channels.json", "description": "Channel resources"},
                {"path": "/bridges.json", "description": "Bridge resources"}
            ]
        })

    async def channels_list(request):
        """Mock channels list endpoint."""
        return web.json_response([])

    async def channels_create(request):
        """Mock channel creation endpoint."""
        params = request.query
        return web.json_response({
            "id": "test_channel_123",
            "name": f"TEST/{params.get('endpoint', 'unknown')}",
            "state": "Down"
        })

    async def bridges_list(request):
        """Mock bridges list endpoint."""
        return web.json_response([])

    async def bridges_create(request):
        """Mock bridge creation endpoint."""
        params = request.query
        return web.json_response({
            "id": "test_bridge_456",
            "technology": "simple_bridge",
            "bridge_type": params.get("type", "mixing"),
            "channels": []
        })

    # Create test application
    app = web.Application()

    # Add routes
    app.router.add_get('/ari/asterisk/info', asterisk_info)
    app.router.add_get('/ari/api-docs/resources.json', api_docs)
    app.router.add_get('/ari/channels', channels_list)
    app.router.add_post('/ari/channels', channels_create)
    app.router.add_get('/ari/bridges', bridges_list)
    app.router.add_post('/ari/bridges', bridges_create)

    return await aiohttp_client(app)


@pytest.fixture
async def mock_ari_client(ari_config: ARIConfig) -> AsyncGenerator[ARIClient, None]:
    """Provide mocked ARI client for testing.

    Args:
        ari_config: ARI configuration fixture

    Yields:
        ARIClient instance with mocked session
    """
    client = ARIClient(ari_config)

    # Mock the session creation
    with patch('aiohttp.ClientSession') as mock_session_class:
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session_class.return_value = mock_session

        # Mock successful connection test
        mock_response = AsyncMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"version": "18.0.0"})

        # Create proper async context manager mock
        mock_context_manager = AsyncMock()
        mock_context_manager.__aenter__.return_value = mock_response
        mock_context_manager.__aexit__.return_value = None
        mock_session.get.return_value = mock_context_manager
        mock_session.post.return_value = mock_context_manager
        mock_session.put.return_value = mock_context_manager
        mock_session.delete.return_value = mock_context_manager

        # Mock TCP connector
        with patch('asterisk_ari.client.TCPConnector') as mock_connector_class:
            mock_connector = AsyncMock()
            mock_connector_class.return_value = mock_connector

            try:
                await client.connect()
                yield client
            finally:
                await client.disconnect()


class MockResponse:
    """Mock response class for testing HTTP interactions."""

    def __init__(
            self,
            status: int = 200,
            json_data: Any = None,
            text_data: str = "",
            content_type: str = "application/json"
    ):
        self.status = status
        self.ok = status < 400
        self.content_type = content_type
        self._json_data = json_data or {}
        self._text_data = text_data

    async def json(self) -> Any:
        """Return JSON data."""
        return self._json_data

    async def text(self) -> str:
        """Return text data."""
        return self._text_data


def create_mock_response(
        status: int = 200,
        json_data: Any = None,
        text_data: str = "",
        content_type: str = "application/json"
) -> MockResponse:
    """Create a mock HTTP response for testing.

    Args:
        status: HTTP status code
        json_data: JSON response data
        text_data: Text response data
        content_type: Response content type

    Returns:
        MockResponse instance
    """
    return MockResponse(status, json_data, text_data, content_type)


# Pytest configuration
def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "asyncio: mark test as an asyncio test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "websocket: mark test as WebSocket related"
    )


# Event loop configuration for async tests
@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
