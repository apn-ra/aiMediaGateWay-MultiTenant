"""Unit tests for ARIClient class.

This module contains comprehensive unit tests for the ARIClient class,
testing connection management, authentication, HTTP operations, and error handling.
"""

import unittest
import pytest
from unittest.mock import AsyncMock, Mock, patch
from typing import Dict, Any, AsyncGenerator
from aiohttp import web
from ari import ARIClient, ARIConfig
from ari.exceptions import (
    ARIError,
    AuthenticationError,
    ConnectionError,
    HTTPError,
    ResourceNotFoundError,
    ValidationError,
)


class TestARIClient:
    """Test cases for ARIClient class."""

    @pytest.mark.asyncio
    async def test_client_initialization(self, ari_config: ARIConfig):
        """Test ARIClient initialization."""
        client = ARIClient(ari_config)

        assert client.config == ari_config
        assert not client.is_connected
        assert client.websocket is None
        assert client.available_endpoints == {}

    @pytest.mark.asyncio
    async def test_client_context_manager(self, ari_config: ARIConfig):
        """Test ARIClient as async context manager."""
        with patch('ari.client.aiohttp.ClientSession') as mock_session_class:
            with patch('ari.client.TCPConnector') as mock_connector_class:
                # Setup mocks
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                mock_connector = AsyncMock()
                mock_connector_class.return_value = mock_connector

                # Mock successful connection test
                mock_response = AsyncMock()
                mock_response.ok = True
                mock_response.status = 200
                mock_response.json = AsyncMock(return_value={"version": "18.0.0"})
                mock_session.get.return_value.__aenter__.return_value = mock_response

                # Test context manager
                async with ARIClient(ari_config) as client:
                    assert client.is_connected
                    assert mock_session_class.called
                    assert mock_connector_class.called

                # Verify cleanup was called
                mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_success(self, ari_config: ARIConfig, mock_asterisk_info: Dict[str, Any]):
        """Test successful connection to Asterisk."""
        client = ARIClient(ari_config)

        with patch('ari.client.aiohttp.ClientSession') as mock_session_class:
            with patch('ari.client.TCPConnector') as mock_connector_class:
                # Setup mocks
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                mock_connector = AsyncMock()
                mock_connector_class.return_value = mock_connector

                # Mock successful responses
                mock_info_response = AsyncMock()
                mock_info_response.ok = True
                mock_info_response.status = 200
                mock_info_response.json = AsyncMock(return_value=mock_asterisk_info)

                mock_docs_response = AsyncMock()
                mock_docs_response.ok = True
                mock_docs_response.status = 200
                mock_docs_response.json = AsyncMock(return_value={"apis": []})

                # Configure mock to return different responses for different URLs
                async def mock_get(url):
                    mock_context = AsyncMock()
                    if "asterisk/info" in url:
                        mock_context.__aenter__.return_value = mock_info_response
                    else:  # api-docs/resources.json
                        mock_context.__aenter__.return_value = mock_docs_response
                    return mock_context

                mock_session.get.side_effect = mock_get

                try:
                    await client.connect()

                    assert client.is_connected
                    assert client._session is not None
                    assert client._connector is not None

                finally:
                    await client.disconnect()

    @pytest.mark.asyncio
    async def test_connection_authentication_failure(self, ari_config: ARIConfig):
        """Test connection failure due to authentication error."""
        client = ARIClient(ari_config)

        with patch('ari.client.aiohttp.ClientSession') as mock_session_class:
            with patch('ari.client.TCPConnector') as mock_connector_class:
                # Setup mocks
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                mock_connector = AsyncMock()
                mock_connector_class.return_value = mock_connector

                # Mock 401 authentication failure
                mock_response = AsyncMock()
                mock_response.ok = False
                mock_response.status = 401
                mock_session.get.return_value.__aenter__.return_value = mock_response

                with pytest.raises(AuthenticationError) as exc_info:
                    await client.connect()

                assert not client.is_connected
                assert exc_info.value.status_code == 401
                assert exc_info.value.username == ari_config.username

    @pytest.mark.asyncio
    async def test_connection_network_error(self, ari_config: ARIConfig):
        """Test connection failure due to network error."""
        client = ARIClient(ari_config)

        with patch('ari.client.aiohttp.ClientSession') as mock_session_class:
            with patch('ari.client.TCPConnector') as mock_connector_class:
                # Setup mocks
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                mock_connector = AsyncMock()
                mock_connector_class.return_value = mock_connector

                # Mock network error
                import aiohttp
                mock_session.get.side_effect = aiohttp.ClientConnectorError(
                    connection_key=Mock(), os_error=OSError("Connection refused")
                )

                with pytest.raises(ConnectionError) as exc_info:
                    await client.connect()

                assert not client.is_connected
                assert "Network error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_get_request(self, mock_ari_client: ARIClient, mock_channel_data: Dict[str, Any]):
        """Test GET request functionality."""
        # Configure mock response
        mock_response = AsyncMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.content_type = "application/json"
        mock_response.json = AsyncMock(return_value=[mock_channel_data])

        mock_ari_client.session.get.return_value.__aenter__.return_value = mock_response

        # Make request
        result = await mock_ari_client.get_channels()

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == mock_channel_data["id"]

        # Verify the request was made correctly
        mock_ari_client.session.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_post_request(self, mock_ari_client: ARIClient, mock_channel_data: Dict[str, Any]):
        """Test POST request functionality."""
        # Configure mock response
        mock_response = AsyncMock()
        mock_response.ok = True
        mock_response.status = 200
        mock_response.content_type = "application/json"
        mock_response.json = AsyncMock(return_value=mock_channel_data)

        mock_ari_client.session.request.return_value.__aenter__.return_value = mock_response

        # Make request
        result = await mock_ari_client.create_channel("SIP/1000")

        assert result["id"] == mock_channel_data["id"]

        # Verify the request was made correctly
        mock_ari_client.session.request.assert_called_once()
        args, kwargs = mock_ari_client.session.request.call_args
        assert args[0] == "POST"
        assert "/channels" in args[1]
        assert "params" in kwargs
        assert kwargs["params"]["endpoint"] == "SIP/1000"

    @pytest.mark.asyncio
    async def test_http_error_handling(self, mock_ari_client: ARIClient):
        """Test HTTP error handling."""
        # Configure mock 404 response
        mock_response = AsyncMock()
        mock_response.ok = False
        mock_response.status = 404
        mock_response.content_type = "application/json"
        mock_response.json = AsyncMock(return_value={"message": "Channel not found"})

        mock_ari_client.session.get.return_value.__aenter__.return_value = mock_response

        # Make request that should fail
        with pytest.raises(ResourceNotFoundError) as exc_info:
            await mock_ari_client.get_channel("nonexistent_channel")

        assert exc_info.value.status_code == 404
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_authentication_error_during_request(self, mock_ari_client: ARIClient):
        """Test authentication error during API request."""
        # Configure mock 401 response
        mock_response = AsyncMock()
        mock_response.ok = False
        mock_response.status = 401
        mock_response.content_type = "application/json"
        mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})

        mock_ari_client.session.get.return_value.__aenter__.return_value = mock_response

        # Make request that should fail with auth error
        with pytest.raises(AuthenticationError) as exc_info:
            await mock_ari_client.get_channels()

        assert exc_info.value.status_code == 401
        assert exc_info.value.username == mock_ari_client.config.username

    @pytest.mark.asyncio
    async def test_disconnected_client_request(self, ari_config: ARIConfig):
        """Test request on disconnected client."""
        client = ARIClient(ari_config)

        # Try to make request without connecting
        with pytest.raises(ConnectionError) as exc_info:
            await client.get_channels()

        assert "not connected" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_channel_operations(self, mock_ari_client: ARIClient, mock_channel_data: Dict[str, Any]):
        """Test basic channel operations."""

        # Mock responses for different operations
        def create_mock_response(data=None, status=200):
            mock_response = AsyncMock()
            mock_response.ok = status < 400
            mock_response.status = status
            mock_response.content_type = "application/json"
            if data:
                mock_response.json = AsyncMock(return_value=data)
            return mock_response

        # Test get_channels
        mock_ari_client.session.get.return_value.__aenter__.return_value = create_mock_response([mock_channel_data])
        channels = await mock_ari_client.get_channels()
        assert len(channels) == 1
        assert channels[0]["id"] == mock_channel_data["id"]

        # Test get_channel
        mock_ari_client.session.get.return_value.__aenter__.return_value = create_mock_response(mock_channel_data)
        channel = await mock_ari_client.get_channel("channel_id_123")
        assert channel["id"] == mock_channel_data["id"]

        # Test create_channel
        mock_ari_client.session.request.return_value.__aenter__.return_value = create_mock_response(mock_channel_data)
        new_channel = await mock_ari_client.create_channel("SIP/1000")
        assert new_channel["id"] == mock_channel_data["id"]

        # Test answer_channel (returns no content)
        mock_ari_client.session.request.return_value.__aenter__.return_value = create_mock_response(status=204)
        await mock_ari_client.answer_channel("channel_id_123")  # Should not raise

        # Test hangup_channel (returns no content)
        mock_ari_client.session.request.return_value.__aenter__.return_value = create_mock_response(status=204)
        await mock_ari_client.hangup_channel("channel_id_123")  # Should not raise

    @pytest.mark.asyncio
    async def test_bridge_operations(self, mock_ari_client: ARIClient, mock_bridge_data: Dict[str, Any]):
        """Test basic bridge operations."""

        def create_mock_response(data=None, status=200):
            mock_response = AsyncMock()
            mock_response.ok = status < 400
            mock_response.status = status
            mock_response.content_type = "application/json"
            if data:
                mock_response.json = AsyncMock(return_value=data)
            return mock_response

        # Test get_bridges
        mock_ari_client.session.get.return_value.__aenter__.return_value = create_mock_response([mock_bridge_data])
        bridges = await mock_ari_client.get_bridges()
        assert len(bridges) == 1
        assert bridges[0]["id"] == mock_bridge_data["id"]

        # Test get_bridge
        mock_ari_client.session.get.return_value.__aenter__.return_value = create_mock_response(mock_bridge_data)
        bridge = await mock_ari_client.get_bridge("bridge_id_456")
        assert bridge["id"] == mock_bridge_data["id"]

        # Test create_bridge
        mock_ari_client.session.request.return_value.__aenter__.return_value = create_mock_response(mock_bridge_data)
        new_bridge = await mock_ari_client.create_bridge("mixing")
        assert new_bridge["id"] == mock_bridge_data["id"]

    @pytest.mark.asyncio
    async def test_endpoint_discovery_success(self, ari_config: ARIConfig):
        """Test successful endpoint discovery."""
        client = ARIClient(ari_config)

        with patch('asterisk_ari.client.aiohttp.ClientSession') as mock_session_class:
            with patch('asterisk_ari.client.TCPConnector') as mock_connector_class:
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                mock_connector = AsyncMock()
                mock_connector_class.return_value = mock_connector

                # Mock successful connection and endpoint discovery
                mock_info_response = AsyncMock()
                mock_info_response.ok = True
                mock_info_response.status = 200
                mock_info_response.json = AsyncMock(return_value={"version": "18.0.0"})

                mock_docs_response = AsyncMock()
                mock_docs_response.ok = True
                mock_docs_response.status = 200
                endpoints_data = {
                    "swaggerVersion": "1.2",
                    "apis": [
                        {"path": "/channels.json", "description": "Channel resources"},
                        {"path": "/bridges.json", "description": "Bridge resources"}
                    ]
                }
                mock_docs_response.json = AsyncMock(return_value=endpoints_data)

                # Configure responses based on URL
                async def mock_get(url):
                    mock_context = AsyncMock()
                    if "asterisk/info" in url:
                        mock_context.__aenter__.return_value = mock_info_response
                    else:  # api-docs/resources.json
                        mock_context.__aenter__.return_value = mock_docs_response
                    return mock_context

                mock_session.get.side_effect = mock_get

                try:
                    await client.connect()

                    assert client.available_endpoints == endpoints_data

                finally:
                    await client.disconnect()

    @pytest.mark.asyncio
    async def test_endpoint_discovery_failure(self, ari_config: ARIConfig):
        """Test graceful handling of endpoint discovery failure."""
        client = ARIClient(ari_config)

        with patch('asterisk_ari.client.aiohttp.ClientSession') as mock_session_class:
            with patch('asterisk_ari.client.TCPConnector') as mock_connector_class:
                mock_session = AsyncMock()
                mock_session.closed = False
                mock_session_class.return_value = mock_session

                mock_connector = AsyncMock()
                mock_connector_class.return_value = mock_connector

                # Mock successful connection but failed endpoint discovery
                mock_info_response = AsyncMock()
                mock_info_response.ok = True
                mock_info_response.status = 200
                mock_info_response.json = AsyncMock(return_value={"version": "18.0.0"})

                mock_docs_response = AsyncMock()
                mock_docs_response.ok = False
                mock_docs_response.status = 404

                # Configure responses based on URL
                async def mock_get(url):
                    mock_context = AsyncMock()
                    if "asterisk/info" in url:
                        mock_context.__aenter__.return_value = mock_info_response
                    else:  # api-docs/resources.json
                        mock_context.__aenter__.return_value = mock_docs_response
                    return mock_context

                mock_session.get.side_effect = mock_get

                try:
                    # Should connect successfully despite endpoint discovery failure
                    await client.connect()

                    assert client.is_connected
                    assert client.available_endpoints == {}  # Empty due to discovery failure

                finally:
                    await client.disconnect()


class TestARIClientProperties:
    """Test ARIClient property methods."""

    def test_config_properties(self, ari_config: ARIConfig):
        """Test configuration-related properties."""
        client = ARIClient(ari_config)
        assert client.config == ari_config

    @pytest.mark.asyncio
    async def test_session_property_disconnected(self, ari_config: ARIConfig):
        """Test session property when disconnected."""
        client = ARIClient(ari_config)

        with pytest.raises(ConnectionError):
            _ = client.session

    def test_is_connected_property(self, ari_config: ARIConfig):
        """Test is_connected property."""
        client = ARIClient(ari_config)
        assert not client.is_connected

    def test_websocket_property(self, ari_config: ARIConfig):
        """Test websocket property."""
        client = ARIClient(ari_config)
        assert client.websocket is None

    def test_available_endpoints_property(self, ari_config: ARIConfig):
        """Test available_endpoints property."""
        client = ARIClient(ari_config)
        endpoints = client.available_endpoints
        assert isinstance(endpoints, dict)
        assert endpoints == {}

