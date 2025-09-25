"""Resource-oriented API classes for Asterisk ARI resources.

This module provides high-level, intuitive classes for working with ARI resources
like channels, bridges, endpoints, and playbacks. These classes abstract away the
low-level REST API calls and provide a more Pythonic interface.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Union, AsyncGenerator, TYPE_CHECKING
from ari.models import (
    Channel as ChannelModel,
    Bridge as BridgeModel,
    Endpoint as EndpointModel,
    Playback as PlaybackModel,
    Recording as RecordingModel,
    Sound as SoundModel,
    Application as ApplicationModel,
    DeviceStateInfo,
    ChannelState,
    BridgeType,
    PlaybackState,
    RecordingState,
)
from ari.exceptions import ARIError, ResourceNotFoundError, ValidationError

# if TYPE_CHECKING:
#     from ari.client import ARIClient

logger = logging.getLogger(__name__)


class ResourceManager:
    """Base class for managing ARI resources."""

    def __init__(self, client: "ARIClient"):
        """Initialize resource manager.

        Args:
            client: The ARI client instance
        """
        self._client = client
        self._cache: Dict[str, Any] = {}
        self._cache_enabled = True

    def enable_cache(self) -> None:
        """Enable resource caching."""
        self._cache_enabled = True

    def disable_cache(self) -> None:
        """Disable resource caching and clear existing cache."""
        self._cache_enabled = False
        self._cache.clear()

    def clear_cache(self, resource_id: Optional[str] = None) -> None:
        """Clear resource cache.

        Args:
            resource_id: Specific resource ID to clear, or None to clear all
        """
        if resource_id:
            self._cache.pop(resource_id, None)
        else:
            self._cache.clear()

    def _get_cached(self, resource_id: str) -> Optional[Any]:
        """Get resource from cache."""
        if not self._cache_enabled:
            return None
        return self._cache.get(resource_id)

    def _set_cached(self, resource_id: str, resource: Any) -> None:
        """Store resource in cache."""
        if self._cache_enabled:
            self._cache[resource_id] = resource


class ChannelResource:
    """High-level interface for working with Asterisk channels."""

    def __init__(self, client: "ARIClient", data: Union[Dict[str, Any], ChannelModel]):
        """Initialize channel resource.

        Args:
            client: The ARI client instance
            data: Channel data dictionary or model instance
        """
        self._client = client
        if isinstance(data, dict):
            self._model = ChannelModel.from_dict(data)
        else:
            self._model = data

    @property
    def id(self) -> str:
        """Channel ID."""
        return self._model.id

    @property
    def name(self) -> str:
        """Channel name."""
        return self._model.name

    @property
    def state(self) -> ChannelState:
        """Channel state."""
        return self._model.state

    @property
    def model(self) -> ChannelModel:
        """Underlying Pydantic model."""
        return self._model

    async def refresh(self) -> None:
        """Refresh channel data from Asterisk."""
        try:
            data = await self._client.get(f"/channels/{self.id}")
            self._model = ChannelModel.from_dict(data)
            logger.debug(f"Refreshed channel {self.id}")
        except Exception as e:
            logger.error(f"Failed to refresh channel {self.id}: {e}")
            raise

    async def answer(self) -> None:
        """Answer the channel."""
        if self.state in [ChannelState.UP]:
            logger.warning(f"Channel {self.id} is already answered")
            return

        try:
            await self._client.post(f"/channels/{self.id}/answer")
            await self.refresh()
            logger.info(f"Answered channel {self.id}")
        except Exception as e:
            logger.error(f"Failed to answer channel {self.id}: {e}")
            raise

    async def hangup(self, reason: str = "normal") -> None:
        """Hangup the channel.

        Args:
            reason: Reason for hangup (normal, busy, congestion, etc.)
        """
        try:
            await self._client.delete(f"/channels/{self.id}", params={"reason": reason})
            logger.info(f"Hung up channel {self.id} with reason: {reason}")
        except Exception as e:
            logger.error(f"Failed to hangup channel {self.id}: {e}")
            raise

    async def play(self, media: str, language: Optional[str] = None) -> "PlaybackResource":
        """Play media to the channel.

        Args:
            media: Media URI to play (e.g., "sound:hello-world")
            language: Language for the media

        Returns:
            PlaybackResource instance for controlling playback
        """
        try:
            params = {"media": media}
            if language:
                params["lang"] = language

            data = await self._client.post(f"/channels/{self.id}/play", json=params)
            logger.info(f"Started playback on channel {self.id}: {media}")
            return PlaybackResource(self._client, data)
        except Exception as e:
            logger.error(f"Failed to play media on channel {self.id}: {e}")
            raise

    async def record(self, name: str, format: str = "wav",
                     max_duration: Optional[int] = None,
                     max_silence: Optional[int] = None) -> "RecordingResource":
        """Start recording the channel.

        Args:
            name: Name of the recording
            format: Recording format (wav, gsm, etc.)
            max_duration: Maximum duration in seconds
            max_silence: Maximum silence duration in seconds

        Returns:
            RecordingResource instance for controlling recording
        """
        try:
            params = {
                "name": name,
                "format": format
            }
            if max_duration:
                params["maxDurationSeconds"] = max_duration
            if max_silence:
                params["maxSilenceSeconds"] = max_silence

            data = await self._client.post(f"/channels/{self.id}/record", json=params)
            logger.info(f"Started recording channel {self.id}: {name}")
            return RecordingResource(self._client, data)
        except Exception as e:
            logger.error(f"Failed to start recording on channel {self.id}: {e}")
            raise

    async def send_dtmf(self, dtmf: str, duration: Optional[int] = None) -> None:
        """Send DTMF tones to the channel.

        Args:
            dtmf: DTMF string to send
            duration: Duration of each tone in milliseconds
        """
        try:
            params = {"dtmf": dtmf}
            if duration:
                params["duration"] = duration

            await self._client.post(f"/channels/{self.id}/dtmf", json=params)
            logger.info(f"Sent DTMF to channel {self.id}: {dtmf}")
        except Exception as e:
            logger.error(f"Failed to send DTMF to channel {self.id}: {e}")
            raise

    async def mute(self, direction: str = "both") -> None:
        """Mute the channel.

        Args:
            direction: Direction to mute (in, out, both)
        """
        try:
            await self._client.post(f"/channels/{self.id}/mute",
                                    json={"direction": direction})
            logger.info(f"Muted channel {self.id} in direction: {direction}")
        except Exception as e:
            logger.error(f"Failed to mute channel {self.id}: {e}")
            raise

    async def unmute(self, direction: str = "both") -> None:
        """Unmute the channel.

        Args:
            direction: Direction to unmute (in, out, both)
        """
        try:
            await self._client.post(f"/channels/{self.id}/unmute",
                                    json={"direction": direction})
            logger.info(f"Unmuted channel {self.id} in direction: {direction}")
        except Exception as e:
            logger.error(f"Failed to unmute channel {self.id}: {e}")
            raise

    async def hold(self) -> None:
        """Put the channel on hold."""
        try:
            await self._client.post(f"/channels/{self.id}/hold")
            logger.info(f"Put channel {self.id} on hold")
        except Exception as e:
            logger.error(f"Failed to hold channel {self.id}: {e}")
            raise

    async def unhold(self) -> None:
        """Remove the channel from hold."""
        try:
            await self._client.post(f"/channels/{self.id}/unhold")
            logger.info(f"Removed channel {self.id} from hold")
        except Exception as e:
            logger.error(f"Failed to unhold channel {self.id}: {e}")
            raise

    def __str__(self) -> str:
        return f"Channel({self.id}, {self.name}, {self.state})"

    def __repr__(self) -> str:
        return self.__str__()


class BridgeResource:
    """High-level interface for working with Asterisk bridges."""

    def __init__(self, client: "ARIClient", data: Union[Dict[str, Any], BridgeModel]):
        """Initialize bridge resource.

        Args:
            client: The ARI client instance
            data: Bridge data dictionary or model instance
        """
        self._client = client
        if isinstance(data, dict):
            self._model = BridgeModel.from_dict(data)
        else:
            self._model = data

    @property
    def id(self) -> str:
        """Bridge ID."""
        return self._model.id

    @property
    def technology(self) -> str:
        """Bridge technology."""
        return self._model.technology

    @property
    def bridge_type(self) -> BridgeType:
        """Bridge type."""
        return self._model.bridge_type

    @property
    def channels(self) -> List[str]:
        """Channel IDs in the bridge."""
        return self._model.channels

    @property
    def model(self) -> BridgeModel:
        """Underlying Pydantic model."""
        return self._model

    async def refresh(self) -> None:
        """Refresh bridge data from Asterisk."""
        try:
            data = await self._client.get(f"/bridges/{self.id}")
            self._model = BridgeModel.from_dict(data)
            logger.debug(f"Refreshed bridge {self.id}")
        except Exception as e:
            logger.error(f"Failed to refresh bridge {self.id}: {e}")
            raise

    async def add_channel(self, channel_id: str, role:str = 'participant', absorbDTMF:bool = False, muted:bool = False) -> None:
        """Add a channel to the bridge.

        Args:
            channel_id: Channel ID
            role: Role of the channel in the bridge (default: participant)
            absorbDTMF: Absorb DTMF tones from the channel (default: false)
            muted: Mute the channel (default: false)
        """

        try:
            await self._client.post(f"/bridges/{self.id}/addChannel?channel={channel_id}&role={role}&absorbDTMF={absorbDTMF}&muted={muted}")
            await self.refresh()
            logger.info(f"Added channel {channel_id} to bridge {self.id}")
        except Exception as e:
            logger.error(f"Failed to add channel {channel_id} to bridge {self.id}: {e}")
            raise

    async def remove_channel(self, channel: Union[str, ChannelResource]) -> None:
        """Remove a channel from the bridge.

        Args:
            channel: Channel ID or ChannelResource instance
        """
        channel_id = channel.id if isinstance(channel, ChannelResource) else channel

        try:
            await self._client.post(f"/bridges/{self.id}/removeChannel",
                                    json={"channel": channel_id})
            await self.refresh()
            logger.info(f"Removed channel {channel_id} from bridge {self.id}")
        except Exception as e:
            logger.error(f"Failed to remove channel {channel_id} from bridge {self.id}: {e}")
            raise

    async def play(self, media: str, language: Optional[str] = None) -> "PlaybackResource":
        """Play media to all channels in the bridge.

        Args:
            media: Media URI to play
            language: Language for the media

        Returns:
            PlaybackResource instance for controlling playback
        """
        try:
            params = {"media": media}
            if language:
                params["lang"] = language

            data = await self._client.post(f"/bridges/{self.id}/play", json=params)
            logger.info(f"Started playback on bridge {self.id}: {media}")
            return PlaybackResource(self._client, data)
        except Exception as e:
            logger.error(f"Failed to play media on bridge {self.id}: {e}")
            raise

    async def record(self, name: str, format: str = "wav",
                     max_duration: Optional[int] = None) -> "RecordingResource":
        """Start recording the bridge.

        Args:
            name: Name of the recording
            format: Recording format
            max_duration: Maximum duration in seconds

        Returns:
            RecordingResource instance for controlling recording
        """
        try:
            params = {
                "name": name,
                "format": format
            }
            if max_duration:
                params["maxDurationSeconds"] = max_duration

            data = await self._client.post(f"/bridges/{self.id}/record", json=params)
            logger.info(f"Started recording bridge {self.id}: {name}")
            return RecordingResource(self._client, data)
        except Exception as e:
            logger.error(f"Failed to start recording on bridge {self.id}: {e}")
            raise

    async def destroy(self) -> None:
        """Destroy the bridge."""
        try:
            await self._client.delete(f"/bridges/{self.id}")
            logger.info(f"Destroyed bridge {self.id}")
        except Exception as e:
            logger.error(f"Failed to destroy bridge {self.id}: {e}")
            raise

    def __str__(self) -> str:
        return f"Bridge({self.id}, {self.technology}, channels={len(self.channels)})"

    def __repr__(self) -> str:
        return self.__str__()


class PlaybackResource:
    """High-level interface for controlling media playback."""

    def __init__(self, client: "ARIClient", data: Union[Dict[str, Any], PlaybackModel]):
        """Initialize playback resource.

        Args:
            client: The ARI client instance
            data: Playback data dictionary or model instance
        """
        self._client = client
        if isinstance(data, dict):
            self._model = PlaybackModel.from_dict(data)
        else:
            self._model = data

    @property
    def id(self) -> str:
        """Playback ID."""
        return self._model.id

    @property
    def state(self) -> PlaybackState:
        """Playback state."""
        return self._model.state

    @property
    def media_uri(self) -> str:
        """Media URI being played."""
        return self._model.media_uri

    @property
    def model(self) -> PlaybackModel:
        """Underlying Pydantic model."""
        return self._model

    async def refresh(self) -> None:
        """Refresh playback data from Asterisk."""
        try:
            data = await self._client.get(f"/playbacks/{self.id}")
            self._model = PlaybackModel.from_dict(data)
            logger.debug(f"Refreshed playback {self.id}")
        except Exception as e:
            logger.error(f"Failed to refresh playback {self.id}: {e}")
            raise

    async def stop(self) -> None:
        """Stop the playback."""
        try:
            await self._client.delete(f"/playbacks/{self.id}")
            logger.info(f"Stopped playback {self.id}")
        except Exception as e:
            logger.error(f"Failed to stop playback {self.id}: {e}")
            raise

    async def control(self, operation: str) -> None:
        """Control playback operation.

        Args:
            operation: Control operation (pause, unpause, restart, forward, reverse)
        """
        try:
            await self._client.post(f"/playbacks/{self.id}/control",
                                    json={"operation": operation})
            logger.info(f"Controlled playback {self.id}: {operation}")
        except Exception as e:
            logger.error(f"Failed to control playback {self.id}: {e}")
            raise

    async def pause(self) -> None:
        """Pause the playback."""
        await self.control("pause")

    async def unpause(self) -> None:
        """Unpause the playback."""
        await self.control("unpause")

    async def restart(self) -> None:
        """Restart the playback."""
        await self.control("restart")

    def __str__(self) -> str:
        return f"Playback({self.id}, {self.state}, {self.media_uri})"

    def __repr__(self) -> str:
        return self.__str__()


class RecordingResource:
    """High-level interface for controlling recordings."""

    def __init__(self, client: "ARIClient", data: Union[Dict[str, Any], RecordingModel]):
        """Initialize recording resource.

        Args:
            client: The ARI client instance
            data: Recording data dictionary or model instance
        """
        self._client = client
        if isinstance(data, dict):
            self._model = RecordingModel.from_dict(data)
        else:
            self._model = data

    @property
    def name(self) -> str:
        """Recording name."""
        return self._model.name

    @property
    def state(self) -> RecordingState:
        """Recording state."""
        return self._model.state

    @property
    def format(self) -> str:
        """Recording format."""
        return self._model.format

    @property
    def model(self) -> RecordingModel:
        """Underlying Pydantic model."""
        return self._model

    async def refresh(self) -> None:
        """Refresh recording data from Asterisk."""
        try:
            data = await self._client.get(f"/recordings/live/{self.name}")
            self._model = RecordingModel.from_dict(data)
            logger.debug(f"Refreshed recording {self.name}")
        except Exception as e:
            logger.error(f"Failed to refresh recording {self.name}: {e}")
            raise

    async def stop(self) -> None:
        """Stop the recording."""
        try:
            await self._client.post(f"/recordings/live/{self.name}/stop")
            logger.info(f"Stopped recording {self.name}")
        except Exception as e:
            logger.error(f"Failed to stop recording {self.name}: {e}")
            raise

    async def pause(self) -> None:
        """Pause the recording."""
        try:
            await self._client.post(f"/recordings/live/{self.name}/pause")
            logger.info(f"Paused recording {self.name}")
        except Exception as e:
            logger.error(f"Failed to pause recording {self.name}: {e}")
            raise

    async def unpause(self) -> None:
        """Unpause the recording."""
        try:
            await self._client.post(f"/recordings/live/{self.name}/unpause")
            logger.info(f"Unpaused recording {self.name}")
        except Exception as e:
            logger.error(f"Failed to unpause recording {self.name}: {e}")
            raise

    async def delete(self) -> None:
        """Delete the recording."""
        try:
            await self._client.delete(f"/recordings/stored/{self.name}")
            logger.info(f"Deleted recording {self.name}")
        except Exception as e:
            logger.error(f"Failed to delete recording {self.name}: {e}")
            raise

    def __str__(self) -> str:
        return f"Recording({self.name}, {self.state}, {self.format})"

    def __repr__(self) -> str:
        return self.__str__()


class EndpointResource:
    """High-level interface for working with endpoints."""

    def __init__(self, client: "ARIClient", data: Union[Dict[str, Any], EndpointModel]):
        """Initialize endpoint resource.

        Args:
            client: The ARI client instance
            data: Endpoint data dictionary or model instance
        """
        self._client = client
        if isinstance(data, dict):
            self._model = EndpointModel.from_dict(data)
        else:
            self._model = data

    @property
    def technology(self) -> str:
        """Endpoint technology."""
        return self._model.technology

    @property
    def resource(self) -> str:
        """Endpoint resource."""
        return self._model.resource

    @property
    def name(self) -> str:
        """Full endpoint name."""
        return self._model.name

    @property
    def state(self) -> Optional[str]:
        """Endpoint state."""
        return self._model.state

    @property
    def model(self) -> EndpointModel:
        """Underlying Pydantic model."""
        return self._model

    async def refresh(self) -> None:
        """Refresh endpoint data from Asterisk."""
        try:
            data = await self._client.get(f"/endpoints/{self.technology}/{self.resource}")
            self._model = EndpointModel.from_dict(data)
            logger.debug(f"Refreshed endpoint {self.name}")
        except Exception as e:
            logger.error(f"Failed to refresh endpoint {self.name}: {e}")
            raise

    def __str__(self) -> str:
        return f"Endpoint({self.name}, {self.state})"

    def __repr__(self) -> str:
        return self.__str__()


# Export all resource classes
__all__ = [
    "ResourceManager",
    "ChannelResource",
    "PlaybackResource",
    "RecordingResource",
    "EndpointResource",
    "BridgeResource"
]
