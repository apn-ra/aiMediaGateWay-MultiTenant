"""Pydantic models for Asterisk ARI resources.

This module contains all the data models for ARI resources, automatically
generated from the OpenAPI schema with additional enhancements for
better Python integration.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union, Literal
from pydantic import BaseModel, Field, validator, root_validator, field_validator, ConfigDict
import json


class ChannelState(str, Enum):
    """Channel state enumeration."""
    DOWN = "Down"
    RESERVED = "Rsrvd"
    OFF_HOOK = "OffHook"
    DIALING = "Dialing"
    RING = "Ring"
    RINGING = "Ringing"
    UP = "Up"
    BUSY = "Busy"
    DIALING_OFFHOOK = "Dialing Offhook"
    PRE_RING = "Pre-ring"
    UNKNOWN = "Unknown"


class BridgeType(str, Enum):
    """Bridge type enumeration."""
    MIXING = "mixing"
    HOLDING = "holding"
    DTMF_EVENTS = "dtmf_events"
    PROXY_MEDIA = "proxy_media"
    VIDEO_SFU = "video_sfu"


class RecordingState(str, Enum):
    """Recording state enumeration."""
    QUEUED = "queued"
    RECORDING = "recording"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class PlaybackState(str, Enum):
    """Playback state enumeration."""
    QUEUED = "queued"
    PLAYING = "playing"
    CONTINUING = "continuing"
    DONE = "done"
    FAILED = "failed"


class DeviceState(str, Enum):
    """Device state enumeration."""
    UNKNOWN = "UNKNOWN"
    NOT_INUSE = "NOT_INUSE"
    INUSE = "INUSE"
    BUSY = "BUSY"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    RINGING = "RINGING"
    RINGINUSE = "RINGINUSE"
    ONHOLD = "ONHOLD"


class ARIModel(BaseModel):
    """Base class for all ARI resource models."""

    model_config = ConfigDict(
        validate_assignment=True,  # enable validation on assignment
        populate_by_name=True,  # allow population by field name or alias
        use_enum_values=True,  # use enum values for serialization
        json_schema_extra={  # extra info for generated JSON schema
            "examples": []
        },
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert model to dictionary with proper serialization."""
        return self.model_dump(by_alias=True, exclude_none=True)

    def to_json(self) -> str:
        """Convert model to JSON string."""
        return self.model_dump_json(by_alias=True, exclude_none=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ARIModel":
        """Create model instance from dictionary."""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> "ARIModel":
        """Create model instance from JSON string."""
        return cls.model_validate_json(json_str)


class CallerID(ARIModel):
    """Caller ID information."""
    name: Optional[str] = Field(None, description="Caller ID name")
    number: Optional[str] = Field(None, description="Caller ID number")


class ChannelConnectedLine(ARIModel):
    """Connected line information for a channel."""
    name: Optional[str] = Field(None, description="Connected line name")
    number: Optional[str] = Field(None, description="Connected line number")


class DialplanCEP(ARIModel):
    """Dialplan Context, Extension, Priority."""
    context: str = Field(..., description="Dialplan context")
    exten: str = Field(..., description="Dialplan extension")
    priority: int = Field(..., description="Dialplan priority", ge=1)


class Channel(ARIModel):
    """A channel within Asterisk."""

    id: str = Field(..., description="Unique identifier for the channel")
    name: str = Field(..., description="Name of the channel")
    state: ChannelState = Field(..., description="Current state of the channel")
    caller: Optional[CallerID] = Field(None, description="Caller ID information")
    connected: Optional[ChannelConnectedLine] = Field(None, description="Connected line information")
    accountcode: Optional[str] = Field(None, description="Account code for billing")
    dialplan: Optional[DialplanCEP] = Field(None, description="Current location in the dialplan")
    creationtime: datetime = Field(..., description="Timestamp when channel was created")
    language: Optional[str] = Field(None, description="Channel language")

    @field_validator('creationtime')
    def parse_datetime(cls, v):
        """Parse datetime from various formats."""
        if isinstance(v, str):
            # Handle ISO format with Z suffix
            if v.endswith('Z'):
                v = v[:-1] + '+00:00'
            return datetime.fromisoformat(v.replace('Z', '+00:00'))
        return v

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "1543372545.1",
                    "name": "SIP/alice-00000001",
                    "state": "Up",
                    "caller": {
                        "name": "Alice",
                        "number": "1000"
                    },
                    "creationtime": "2023-01-01T12:00:00.000Z",
                    "language": "en"
                }
            ]
        }
    )


class Bridge(ARIModel):
    """A bridge within Asterisk."""

    id: str = Field(..., description="Unique identifier for the bridge")
    technology: str = Field(..., description="Bridging technology in use")
    bridge_type: BridgeType = Field(..., alias="bridge_type", description="Type of bridge")
    bridge_class: str = Field(..., alias="bridge_class", description="Class of bridge")
    channels: List[str] = Field(default_factory=list, description="Channel IDs in this bridge")
    name: Optional[str] = Field(None, description="Name of the bridge")
    creator: Optional[str] = Field(None, description="Entity that created the bridge")
    creationtime: datetime = Field(..., description="Timestamp when bridge was created")

    @field_validator('creationtime')
    def parse_datetime(cls, v):
        """Parse datetime from various formats."""
        if isinstance(v, str):
            if v.endswith('Z'):
                v = v[:-1] + '+00:00'
            return datetime.fromisoformat(v.replace('Z', '+00:00'))
        return v

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "bridge1",
                    "technology": "simple_bridge",
                    "bridge_type": "mixing",
                    "bridge_class": "basic",
                    "channels": ["1543372545.1", "1543372545.2"],
                    "creationtime": "2023-01-01T12:00:00.000Z"
                }
            ]
        }
    )


class Endpoint(ARIModel):
    """An endpoint within Asterisk."""

    technology: str = Field(..., description="Technology of the endpoint")
    resource: str = Field(..., description="Resource name/identifier")
    state: Optional[DeviceState] = Field(None, description="State of the endpoint")
    channel_ids: List[str] = Field(default_factory=list, alias="channel_ids",
                                   description="Channel IDs associated with endpoint")

    @property
    def name(self) -> str:
        """Full endpoint name."""
        return f"{self.technology}/{self.resource}"

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "technology": "SIP",
                    "resource": "alice",
                    "state": "NOT_INUSE",
                    "channel_ids": []
                }
            ]
        }
    )


class Playback(ARIModel):
    """A playback operation within Asterisk."""

    id: str = Field(..., description="Unique identifier for the playback")
    media_uri: str = Field(..., alias="media_uri", description="URI of the media being played")
    target_uri: str = Field(..., alias="target_uri", description="URI of the target (channel/bridge)")
    language: Optional[str] = Field(None, description="Language for the playback")
    state: PlaybackState = Field(..., description="Current state of the playback")

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "playback1",
                    "media_uri": "sound:hello-world",
                    "target_uri": "channel:1543372545.1",
                    "state": "playing"
                }
            ]
        }
    )


class Recording(ARIModel):
    """A recording operation within Asterisk."""

    name: str = Field(..., description="Name of the recording")
    format: str = Field(..., description="Format of the recording")
    state: RecordingState = Field(..., description="Current state of the recording")
    duration: Optional[int] = Field(None, description="Duration in seconds", ge=0)
    talking_duration: Optional[int] = Field(None, alias="talking_duration",
                                            description="Duration of talking in seconds", ge=0)
    silence_duration: Optional[int] = Field(None, alias="silence_duration",
                                            description="Duration of silence in seconds", ge=0)
    target_uri: str = Field(..., alias="target_uri", description="URI of the target being recorded")

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "recording1",
                    "format": "wav",
                    "state": "recording",
                    "target_uri": "channel:1543372545.1"
                }
            ]
        }
    )


class Sound(ARIModel):
    """A sound file within Asterisk."""

    id: str = Field(..., description="Sound identifier")
    text: Optional[str] = Field(None, description="Text description of the sound")
    formats: List[str] = Field(default_factory=list, description="Available formats for the sound")

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "hello-world",
                    "text": "Hello World",
                    "formats": ["gsm", "wav", "ulaw"]
                }
            ]
        }
    )


class Application(ARIModel):
    """A Stasis application within Asterisk."""

    name: str = Field(..., description="Name of the application")
    channel_ids: List[str] = Field(default_factory=list, alias="channel_ids",
                                   description="Channel IDs in this application")
    bridge_ids: List[str] = Field(default_factory=list, alias="bridge_ids",
                                  description="Bridge IDs in this application")
    endpoint_ids: List[str] = Field(default_factory=list, alias="endpoint_ids",
                                    description="Endpoint IDs in this application")
    device_names: List[str] = Field(default_factory=list, alias="device_names",
                                    description="Device names subscribed to by this application")

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "hello_world",
                    "channel_ids": ["1543372545.1"],
                    "bridge_ids": [],
                    "endpoint_ids": [],
                    "device_names": []
                }
            ]
        }
    )

class DeviceStateInfo(ARIModel):
    """Device state information."""

    name: str = Field(..., description="Name of the device")
    state: DeviceState = Field(..., description="State of the device")

    model_config = ARIModel.model_config | ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "SIP/alice",
                    "state": "NOT_INUSE"
                }
            ]
        }
    )


# Event models for WebSocket events
class ARIEvent(ARIModel):
    """Base class for all ARI events."""

    type: str = Field(..., description="Type of the event")
    timestamp: datetime = Field(..., description="Timestamp of the event")
    application: str = Field(..., description="Name of the application receiving the event")

    @field_validator('timestamp')
    def parse_datetime(cls, v):
        """Parse datetime from various formats."""
        if isinstance(v, str):
            if v.endswith('Z'):
                v = v[:-1] + '+00:00'
            return datetime.fromisoformat(v.replace('Z', '+00:00'))
        return v


class ChannelCreated(ARIEvent):
    """Event fired when a channel is created."""

    type: Literal["ChannelCreated"] = "ChannelCreated"
    channel: Channel = Field(..., description="The channel that was created")


class ChannelDestroyed(ARIEvent):
    """Event fired when a channel is destroyed."""

    type: Literal["ChannelDestroyed"] = "ChannelDestroyed"
    channel: Channel = Field(..., description="The channel that was destroyed")
    cause: int = Field(..., description="Cause code for the channel destruction")
    cause_txt: str = Field(..., alias="cause_txt", description="Text description of the cause")


class ChannelStateChange(ARIEvent):
    """Event fired when a channel's state changes."""

    type: Literal["ChannelStateChange"] = "ChannelStateChange"
    channel: Channel = Field(..., description="The channel whose state changed")


class BridgeCreated(ARIEvent):
    """Event fired when a bridge is created."""

    type: Literal["BridgeCreated"] = "BridgeCreated"
    bridge: Bridge = Field(..., description="The bridge that was created")


class BridgeDestroyed(ARIEvent):
    """Event fired when a bridge is destroyed."""

    type: Literal["BridgeDestroyed"] = "BridgeDestroyed"
    bridge: Bridge = Field(..., description="The bridge that was destroyed")


class ChannelEnteredBridge(ARIEvent):
    """Event fired when a channel enters a bridge."""

    type: Literal["ChannelEnteredBridge"] = "ChannelEnteredBridge"
    bridge: Bridge = Field(..., description="The bridge the channel entered")
    channel: Channel = Field(..., description="The channel that entered the bridge")


class ChannelLeftBridge(ARIEvent):
    """Event fired when a channel leaves a bridge."""

    type: Literal["ChannelLeftBridge"] = "ChannelLeftBridge"
    bridge: Bridge = Field(..., description="The bridge the channel left")
    channel: Channel = Field(..., description="The channel that left the bridge")


class PlaybackStarted(ARIEvent):
    """Event fired when playback starts."""

    type: Literal["PlaybackStarted"] = "PlaybackStarted"
    playback: Playback = Field(..., description="The playback that started")


class PlaybackFinished(ARIEvent):
    """Event fired when playback finishes."""

    type: Literal["PlaybackFinished"] = "PlaybackFinished"
    playback: Playback = Field(..., description="The playback that finished")


class RecordingStarted(ARIEvent):
    """Event fired when recording starts."""

    type: Literal["RecordingStarted"] = "RecordingStarted"
    recording: Recording = Field(..., description="The recording that started")


class RecordingFinished(ARIEvent):
    """Event fired when recording finishes."""

    type: Literal["RecordingFinished"] = "RecordingFinished"
    recording: Recording = Field(..., description="The recording that finished")


# Export all models
__all__ = [
    # Base classes
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
]
