"""Asterisk ARI Python Library.

An async-first Python library for interacting with Asterisk's REST Interface (ARI)
to build powerful telephony applications.
"""

__version__ = "0.1.0"
__author__ = "APN Development Team"
__email__ = "dev@apntelecom.com"
__license__ = "MIT"

from ari.client import ARIClient
from ari.config import ARIConfig
from ari.exceptions import (
    ARIError,
    AuthenticationError,
    ConnectionError,
    HTTPError,
    ResourceNotFoundError,
    ValidationError,
    WebSocketError,
)
from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
    CircuitBreakerStats,
)

from ari.models import (
    # Base classes
    ARIModel,
    # Enums
    ChannelState,
    BridgeType,
    RecordingState,
    PlaybackState,
    DeviceState,
    # Resource models
    CallerID,
    ChannelConnectedLine,
    DialplanCEP,
    Channel,
    Bridge,
    Endpoint,
    Playback,
    Recording,
    Sound,
    Application,
    DeviceStateInfo,
    # Event models
    ARIEvent,
    ChannelCreated,
    ChannelDestroyed,
    ChannelStateChange,
    BridgeCreated,
    BridgeDestroyed,
    ChannelEnteredBridge,
    ChannelLeftBridge,
    PlaybackStarted,
    PlaybackFinished,
    RecordingStarted,
    RecordingFinished,
)

from ari.resources import (
    ResourceManager,
    ChannelResource,
    BridgeResource,
    PlaybackResource,
    RecordingResource,
    EndpointResource,
)

__all__ = [
    # Core client and config
    "ARIClient",
    "ARIConfig",
    # Exceptions
    "ARIError",
    "AuthenticationError",
    "ConnectionError",
    "HTTPError",
    "ResourceNotFoundError",
    "ValidationError",
    "WebSocketError",
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "CircuitBreakerState",
    "CircuitBreakerStats",
    # Base model classes
    "ARIModel",
    # Enums
    "ChannelState",
    "BridgeType",
    "RecordingState",
    "PlaybackState",
    "DeviceState",
    # Resource models
    "CallerID",
    "ChannelConnectedLine",
    "DialplanCEP",
    "Channel",
    "Bridge",
    "Endpoint",
    "Playback",
    "Recording",
    "Sound",
    "Application",
    "DeviceStateInfo",
    # Event models
    "ARIEvent",
    "ChannelCreated",
    "ChannelDestroyed",
    "ChannelStateChange",
    "BridgeCreated",
    "BridgeDestroyed",
    "ChannelEnteredBridge",
    "ChannelLeftBridge",
    "PlaybackStarted",
    "PlaybackFinished",
    "RecordingStarted",
    "RecordingFinished",
    # Resource classes
    "ResourceManager",
    "ChannelResource",
    "BridgeResource",
    "PlaybackResource",
    "RecordingResource",
    "EndpointResource",
]
