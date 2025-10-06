"""
Real-time audio streaming capabilities for the AI Media Gateway system.

This module provides comprehensive real-time audio streaming functionality
supporting multiple streaming protocols and formats for live audio distribution.

Streaming Methods:
- WebSocket audio streaming (real-time bidirectional)
- HTTP chunked audio streaming (server-sent events)
- MQTT audio message publishing (pub/sub messaging)

Features:
- Multi-tenant streaming isolation
- Adaptive bitrate streaming
- Audio format conversion on-the-fly
- Stream quality monitoring
- Connection management and failover
- Buffering and latency optimization
"""

import asyncio
import logging
import json
import time
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import base64
from collections import deque, defaultdict

from django.utils import timezone
from django.conf import settings
from channels.generic.websocket import AsyncWebsocketConsumer

# Import our audio processing components
from core.junie_codes.audio.audio_conversion import AudioConverter, AudioFormat, AudioSpec
from core.rtp_server import AudioFrame
from core.session.manager import get_session_manager

logger = logging.getLogger(__name__)


class StreamingProtocol(Enum):
    """Supported streaming protocols"""
    WEBSOCKET = "websocket"
    HTTP_CHUNKED = "http_chunked"
    MQTT = "mqtt"
    RTP_RELAY = "rtp_relay"


class StreamingFormat(Enum):
    """Audio streaming formats"""
    PCM_RAW = "pcm_raw"
    PCM_BASE64 = "pcm_base64"
    ULAW = "ulaw"
    ALAW = "alaw"
    WAV = "wav"
    JSON_WRAPPED = "json_wrapped"


class StreamQuality(Enum):
    """Stream quality levels"""
    LOW = "low"       # 8kHz, 8-bit
    MEDIUM = "medium" # 16kHz, 16-bit
    HIGH = "high"     # 44kHz, 16-bit
    ADAPTIVE = "adaptive"  # Dynamic based on connection


@dataclass
class StreamingConfig:
    """Configuration for audio streaming"""
    protocol: StreamingProtocol
    format: StreamingFormat
    quality: StreamQuality
    buffer_size_ms: int = 100
    max_latency_ms: int = 200
    enable_compression: bool = True
    sample_rate: int = 8000
    bit_depth: int = 16
    channels: int = 1
    
    def __post_init__(self):
        """Set quality-based defaults"""
        if self.quality == StreamQuality.LOW:
            self.sample_rate = 8000
            self.bit_depth = 8
        elif self.quality == StreamQuality.MEDIUM:
            self.sample_rate = 16000
            self.bit_depth = 16
        elif self.quality == StreamQuality.HIGH:
            self.sample_rate = 44100
            self.bit_depth = 16


@dataclass
class StreamConnection:
    """Active streaming connection"""
    connection_id: str
    session_id: str
    tenant_id: str
    protocol: StreamingProtocol
    config: StreamingConfig
    created_at: datetime
    last_activity: datetime
    bytes_sent: int = 0
    packets_sent: int = 0
    connection_quality: float = 1.0
    websocket_consumer: Optional[Any] = None
    http_response: Optional[Any] = None
    mqtt_client: Optional[Any] = None


class AudioStreamBuffer:
    """Buffer for audio streaming with adaptive sizing"""
    
    def __init__(self, max_size_ms: int = 500, target_latency_ms: int = 100):
        self.max_size_ms = max_size_ms
        self.target_latency_ms = target_latency_ms
        self.buffer = deque()
        self.buffer_size_bytes = 0
        self.last_packet_time = None
        self.stats = {
            'packets_added': 0,
            'packets_dropped': 0,
            'buffer_overflows': 0,
            'average_latency_ms': 0
        }
    
    def add_audio_frame(self, frame: AudioFrame) -> bool:
        """Add audio frame to buffer"""
        try:
            current_time = timezone.now()
            
            # Check for buffer overflow
            if self._calculate_buffer_latency_ms() > self.max_size_ms:
                self._drop_oldest_frames()
                self.stats['buffer_overflows'] += 1
            
            # Add frame with timestamp
            self.buffer.append((frame, current_time))
            self.buffer_size_bytes += len(frame.payload)
            self.stats['packets_added'] += 1
            
            self.last_packet_time = current_time
            return True
            
        except Exception as e:
            logger.error(f"Error adding audio frame to buffer: {e}")
            return False
    
    def get_next_frame(self) -> Optional[Tuple[AudioFrame, datetime]]:
        """Get next frame from buffer"""
        try:
            if not self.buffer:
                return None
            
            frame, timestamp = self.buffer.popleft()
            self.buffer_size_bytes -= len(frame.payload)
            
            # Update latency statistics
            current_time = timezone.now()
            latency_ms = (current_time - timestamp).total_seconds() * 1000
            self._update_latency_stats(latency_ms)
            
            return frame, timestamp
            
        except Exception as e:
            logger.error(f"Error getting frame from buffer: {e}")
            return None
    
    def _calculate_buffer_latency_ms(self) -> float:
        """Calculate current buffer latency in milliseconds"""
        if not self.buffer:
            return 0.0
        
        oldest_timestamp = self.buffer[0][1]
        current_time = timezone.now()
        return (current_time - oldest_timestamp).total_seconds() * 1000
    
    def _drop_oldest_frames(self, count: int = 1):
        """Drop oldest frames from buffer"""
        for _ in range(min(count, len(self.buffer))):
            if self.buffer:
                frame, _ = self.buffer.popleft()
                self.buffer_size_bytes -= len(frame.payload)
                self.stats['packets_dropped'] += 1
    
    def _update_latency_stats(self, latency_ms: float):
        """Update running latency statistics"""
        if self.stats['average_latency_ms'] == 0:
            self.stats['average_latency_ms'] = latency_ms
        else:
            # Exponential moving average
            alpha = 0.1
            self.stats['average_latency_ms'] = (
                alpha * latency_ms + (1 - alpha) * self.stats['average_latency_ms']
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics"""
        return {
            **self.stats,
            'current_buffer_size_bytes': self.buffer_size_bytes,
            'current_buffer_frames': len(self.buffer),
            'current_latency_ms': self._calculate_buffer_latency_ms()
        }


class WebSocketAudioStreamer:
    """WebSocket-based real-time audio streaming"""
    
    def __init__(self):
        self.active_connections: Dict[str, StreamConnection] = {}
        self.connection_buffers: Dict[str, AudioStreamBuffer] = {}
        self.audio_converter = AudioConverter()
        self.stats = {
            'total_connections': 0,
            'active_connections': 0,
            'total_bytes_streamed': 0,
            'connection_errors': 0
        }
    
    async def add_connection(self, connection: StreamConnection) -> bool:
        """Add new WebSocket connection for streaming"""
        try:
            self.active_connections[connection.connection_id] = connection
            self.connection_buffers[connection.connection_id] = AudioStreamBuffer(
                max_size_ms=connection.config.buffer_size_ms * 2,
                target_latency_ms=connection.config.buffer_size_ms
            )
            
            self.stats['total_connections'] += 1
            self.stats['active_connections'] += 1
            
            logger.info(f"WebSocket connection added: {connection.connection_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding WebSocket connection: {e}")
            return False
    
    async def remove_connection(self, connection_id: str) -> bool:
        """Remove WebSocket connection"""
        try:
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]
                
            if connection_id in self.connection_buffers:
                del self.connection_buffers[connection_id]
                
            self.stats['active_connections'] = max(0, self.stats['active_connections'] - 1)
            
            logger.info(f"WebSocket connection removed: {connection_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error removing WebSocket connection: {e}")
            return False
    
    async def stream_audio_frame(self, session_id: str, frame: AudioFrame) -> int:
        """Stream audio frame to all connections for session"""
        streamed_count = 0
        
        try:
            # Find connections for this session
            session_connections = [
                conn for conn in self.active_connections.values()
                if conn.session_id == session_id
            ]
            
            if not session_connections:
                return 0
            
            # Stream to each connection
            for connection in session_connections:
                try:
                    success = await self._stream_to_connection(connection, frame)
                    if success:
                        streamed_count += 1
                except Exception as e:
                    logger.error(f"Error streaming to connection {connection.connection_id}: {e}")
                    self.stats['connection_errors'] += 1
            
            return streamed_count
            
        except Exception as e:
            logger.error(f"Error streaming audio frame: {e}")
            return 0
    
    async def _stream_to_connection(self, connection: StreamConnection, frame: AudioFrame) -> bool:
        """Stream audio frame to specific connection"""
        try:
            # Add to buffer first
            buffer = self.connection_buffers.get(connection.connection_id)
            if not buffer:
                return False
            
            buffer.add_audio_frame(frame)
            
            # Process and send buffered frames
            return await self._process_buffer_for_connection(connection)
            
        except Exception as e:
            logger.error(f"Error streaming to connection {connection.connection_id}: {e}")
            return False
    
    async def _process_buffer_for_connection(self, connection: StreamConnection) -> bool:
        """Process buffer and send audio data"""
        try:
            buffer = self.connection_buffers.get(connection.connection_id)
            if not buffer:
                return False
            
            frames_sent = 0
            while True:
                frame_data = buffer.get_next_frame()
                if not frame_data:
                    break
                
                frame, timestamp = frame_data
                
                # Convert audio format if needed
                converted_audio = await self._convert_audio_for_streaming(
                    frame.payload, frame.codec, connection.config
                )
                
                if not converted_audio:
                    continue
                
                # Send via WebSocket
                success = await self._send_websocket_data(connection, converted_audio, frame)
                if success:
                    frames_sent += 1
                    connection.bytes_sent += len(converted_audio)
                    connection.packets_sent += 1
                    connection.last_activity = timezone.now()
                else:
                    break
            
            self.stats['total_bytes_streamed'] += connection.bytes_sent
            return frames_sent > 0
            
        except Exception as e:
            logger.error(f"Error processing buffer for connection: {e}")
            return False
    
    async def _convert_audio_for_streaming(
        self, 
        audio_data: bytes, 
        source_codec: str, 
        config: StreamingConfig
    ) -> Optional[bytes]:
        """Convert audio data for streaming format"""
        try:
            # Determine source format
            source_format = AudioFormat.ULAW if source_codec == 'ulaw' else AudioFormat.PCM_LINEAR
            
            # Determine target format
            target_format_map = {
                StreamingFormat.PCM_RAW: AudioFormat.PCM_LINEAR,
                StreamingFormat.PCM_BASE64: AudioFormat.PCM_LINEAR,
                StreamingFormat.ULAW: AudioFormat.ULAW,
                StreamingFormat.ALAW: AudioFormat.ALAW,
                StreamingFormat.WAV: AudioFormat.WAV
            }
            
            target_format = target_format_map.get(config.format, AudioFormat.PCM_LINEAR)
            
            # Create audio specs
            source_spec = AudioSpec(
                format=source_format,
                sample_rate=8000,  # Default for telephony
                bit_depth=16,
                channels=1
            )
            
            target_spec = AudioSpec(
                format=target_format,
                sample_rate=config.sample_rate,
                bit_depth=config.bit_depth,
                channels=config.channels
            )
            
            # Convert audio
            result = self.audio_converter.convert(audio_data, source_spec, target_spec)
            
            if not result.success:
                logger.warning(f"Audio conversion failed: {result.error_message}")
                return None
            
            # Apply format-specific encoding
            if config.format == StreamingFormat.PCM_BASE64:
                return base64.b64encode(result.data)
            elif config.format == StreamingFormat.JSON_WRAPPED:
                return json.dumps({
                    'audio_data': base64.b64encode(result.data).decode(),
                    'format': target_format.value,
                    'sample_rate': config.sample_rate,
                    'timestamp': time.time()
                }).encode()
            
            return result.data
            
        except Exception as e:
            logger.error(f"Error converting audio for streaming: {e}")
            return None
    
    async def _send_websocket_data(
        self, 
        connection: StreamConnection, 
        audio_data: bytes, 
        frame: AudioFrame
    ) -> bool:
        """Send audio data via WebSocket"""
        try:
            if not connection.websocket_consumer:
                return False
            
            # Create message based on format
            if connection.config.format == StreamingFormat.JSON_WRAPPED:
                message = audio_data.decode()
                await connection.websocket_consumer.send(text_data=message)
            else:
                await connection.websocket_consumer.send(bytes_data=audio_data)
            
            return True
            
        except Exception as e:
            logger.error(f"Error sending WebSocket data: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get streaming statistics"""
        connection_stats = {}
        for conn_id, buffer in self.connection_buffers.items():
            connection_stats[conn_id] = buffer.get_stats()
        
        return {
            **self.stats,
            'connection_buffers': connection_stats
        }


class HTTPChunkedAudioStreamer:
    """HTTP chunked transfer encoding for audio streaming"""
    
    def __init__(self):
        self.active_streams: Dict[str, StreamConnection] = {}
        self.stream_buffers: Dict[str, AudioStreamBuffer] = {}
        self.audio_converter = AudioConverter()
        self.stats = {
            'total_streams': 0,
            'active_streams': 0,
            'total_bytes_streamed': 0,
            'stream_errors': 0
        }
    
    async def create_stream(self, connection: StreamConnection) -> bool:
        """Create new HTTP chunked stream"""
        try:
            self.active_streams[connection.connection_id] = connection
            self.stream_buffers[connection.connection_id] = AudioStreamBuffer()
            
            self.stats['total_streams'] += 1
            self.stats['active_streams'] += 1
            
            # Start streaming task
            asyncio.create_task(self._stream_loop(connection))
            
            logger.info(f"HTTP chunked stream created: {connection.connection_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error creating HTTP stream: {e}")
            return False
    
    async def _stream_loop(self, connection: StreamConnection):
        """Main streaming loop for HTTP chunked transfer"""
        try:
            response = connection.http_response
            if not response:
                return
            
            # Set chunked transfer headers
            response['Content-Type'] = 'audio/wav'
            response['Transfer-Encoding'] = 'chunked'
            response['Cache-Control'] = 'no-cache'
            response['Connection'] = 'keep-alive'
            
            buffer = self.stream_buffers[connection.connection_id]
            
            while connection.connection_id in self.active_streams:
                try:
                    frame_data = buffer.get_next_frame()
                    if not frame_data:
                        await asyncio.sleep(0.01)  # 10ms sleep
                        continue
                    
                    frame, timestamp = frame_data
                    
                    # Convert and send audio
                    audio_data = await self._convert_audio_for_streaming(
                        frame.payload, frame.codec, connection.config
                    )
                    
                    if audio_data:
                        chunk_size = hex(len(audio_data))[2:].encode() + b'\r\n'
                        chunk_data = audio_data + b'\r\n'
                        
                        await response.write(chunk_size + chunk_data)
                        
                        connection.bytes_sent += len(audio_data)
                        connection.packets_sent += 1
                        connection.last_activity = timezone.now()
                        
                except Exception as e:
                    logger.error(f"Error in HTTP streaming loop: {e}")
                    break
            
        except Exception as e:
            logger.error(f"HTTP streaming loop error: {e}")
        finally:
            await self.close_stream(connection.connection_id)
    
    async def close_stream(self, connection_id: str):
        """Close HTTP chunked stream"""
        try:
            if connection_id in self.active_streams:
                connection = self.active_streams[connection_id]
                
                # Send final chunk
                if connection.http_response:
                    await connection.http_response.write(b'0\r\n\r\n')
                
                del self.active_streams[connection_id]
                
            if connection_id in self.stream_buffers:
                del self.stream_buffers[connection_id]
                
            self.stats['active_streams'] = max(0, self.stats['active_streams'] - 1)
            
        except Exception as e:
            logger.error(f"Error closing HTTP stream: {e}")


class MQTTAudioPublisher:
    """MQTT-based audio message publishing"""
    
    def __init__(self):
        self.mqtt_client = None
        self.active_subscriptions: Dict[str, StreamConnection] = {}
        self.audio_converter = AudioConverter()
        self.stats = {
            'messages_published': 0,
            'total_bytes_published': 0,
            'publish_errors': 0
        }
        
        self._initialize_mqtt_client()
    
    def _initialize_mqtt_client(self):
        """Initialize MQTT client"""
        try:
            import paho.mqtt.client as mqtt
            
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_publish = self._on_mqtt_publish
            
            # Connect to MQTT broker
            broker_host = getattr(settings, 'MQTT_BROKER_HOST', 'localhost')
            broker_port = getattr(settings, 'MQTT_BROKER_PORT', 1883)
            
            self.mqtt_client.connect(broker_host, broker_port, 60)
            self.mqtt_client.loop_start()
            
        except ImportError:
            logger.warning("paho-mqtt not installed, MQTT streaming disabled")
        except Exception as e:
            logger.error(f"Error initializing MQTT client: {e}")
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            logger.info("Connected to MQTT broker")
        else:
            logger.error(f"Failed to connect to MQTT broker: {rc}")
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        logger.info("Disconnected from MQTT broker")
    
    def _on_mqtt_publish(self, client, userdata, mid):
        """MQTT publish callback"""
        self.stats['messages_published'] += 1
    
    async def publish_audio_frame(self, session_id: str, tenant_id: str, frame: AudioFrame) -> bool:
        """Publish audio frame to MQTT topic"""
        try:
            if not self.mqtt_client:
                return False
            
            # Create MQTT topic with tenant isolation
            topic = f"audio/{tenant_id}/{session_id}"
            
            # Convert audio to base64 for MQTT transport
            audio_data = base64.b64encode(frame.payload).decode()
            
            # Create message payload
            message = {
                'session_id': session_id,
                'tenant_id': tenant_id,
                'timestamp': frame.processed_time.isoformat() if frame.processed_time else None,
                'sequence_number': frame.sequence_number,
                'codec': frame.codec,
                'audio_data': audio_data,
                'metadata': {
                    'rtp_timestamp': frame.timestamp,
                    'payload_size': len(frame.payload)
                }
            }
            
            # Publish message
            result = self.mqtt_client.publish(topic, json.dumps(message), qos=1)
            
            if result.rc == 0:
                self.stats['total_bytes_published'] += len(frame.payload)
                return True
            else:
                self.stats['publish_errors'] += 1
                return False
                
        except Exception as e:
            logger.error(f"Error publishing MQTT audio frame: {e}")
            self.stats['publish_errors'] += 1
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get MQTT publishing statistics"""
        return self.stats.copy()


class AudioStreamingManager:
    """Central manager for all audio streaming operations"""
    
    def __init__(self):
        self.websocket_streamer = WebSocketAudioStreamer()
        self.http_streamer = HTTPChunkedAudioStreamer()
        self.mqtt_publisher = MQTTAudioPublisher()
        
        self.active_sessions: Set[str] = set()
        self.session_connections: Dict[str, List[StreamConnection]] = defaultdict(list)
        
        self.stats = {
            'total_sessions': 0,
            'active_sessions': 0,
            'total_connections': 0,
            'total_bytes_streamed': 0
        }
    
    async def register_session(self, session_id: str) -> bool:
        """Register session for audio streaming"""
        try:
            if session_id not in self.active_sessions:
                self.active_sessions.add(session_id)
                self.stats['total_sessions'] += 1
                self.stats['active_sessions'] += 1
                
                logger.info(f"Session registered for streaming: {session_id}")
                return True
            
            return True
            
        except Exception as e:
            logger.error(f"Error registering session: {e}")
            return False
    
    async def unregister_session(self, session_id: str) -> bool:
        """Unregister session from streaming"""
        try:
            if session_id in self.active_sessions:
                self.active_sessions.remove(session_id)
                self.stats['active_sessions'] = max(0, self.stats['active_sessions'] - 1)
                
                # Close all connections for this session
                if session_id in self.session_connections:
                    for connection in self.session_connections[session_id]:
                        await self._close_connection(connection)
                    
                    del self.session_connections[session_id]
                
                logger.info(f"Session unregistered from streaming: {session_id}")
                return True
            
            return True
            
        except Exception as e:
            logger.error(f"Error unregistering session: {e}")
            return False
    
    async def add_streaming_connection(self, connection: StreamConnection) -> bool:
        """Add new streaming connection"""
        try:
            # Ensure session is registered
            await self.register_session(connection.session_id)
            
            # Add to appropriate streamer
            success = False
            if connection.protocol == StreamingProtocol.WEBSOCKET:
                success = await self.websocket_streamer.add_connection(connection)
            elif connection.protocol == StreamingProtocol.HTTP_CHUNKED:
                success = await self.http_streamer.create_stream(connection)
            
            if success:
                self.session_connections[connection.session_id].append(connection)
                self.stats['total_connections'] += 1
                
                logger.info(f"Streaming connection added: {connection.connection_id}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error adding streaming connection: {e}")
            return False
    
    async def stream_audio_frame(self, session_id: str, frame: AudioFrame) -> bool:
        """Stream audio frame to all active connections"""
        try:
            if session_id not in self.active_sessions:
                return False
            
            total_streamed = 0
            
            # Stream via WebSocket
            ws_count = await self.websocket_streamer.stream_audio_frame(session_id, frame)
            total_streamed += ws_count
            
            # Publish via MQTT if enabled
            if self.mqtt_publisher.mqtt_client:
                # Get tenant_id from session
                session_manager = get_session_manager()
                session_data = await session_manager.get_session(session_id)
                if session_data:
                    tenant_id = session_data.get('tenant_id')
                    if tenant_id:
                        await self.mqtt_publisher.publish_audio_frame(session_id, tenant_id, frame)
                        total_streamed += 1
            
            if total_streamed > 0:
                self.stats['total_bytes_streamed'] += len(frame.payload) * total_streamed
            
            return total_streamed > 0
            
        except Exception as e:
            logger.error(f"Error streaming audio frame: {e}")
            return False
    
    async def _close_connection(self, connection: StreamConnection):
        """Close specific streaming connection"""
        try:
            if connection.protocol == StreamingProtocol.WEBSOCKET:
                await self.websocket_streamer.remove_connection(connection.connection_id)
            elif connection.protocol == StreamingProtocol.HTTP_CHUNKED:
                await self.http_streamer.close_stream(connection.connection_id)
            
        except Exception as e:
            logger.error(f"Error closing connection: {e}")
    
    def get_comprehensive_stats(self) -> Dict[str, Any]:
        """Get comprehensive streaming statistics"""
        return {
            'manager': self.stats,
            'websocket': self.websocket_streamer.get_stats(),
            'http_chunked': self.http_streamer.stats,
            'mqtt': self.mqtt_publisher.get_stats(),
            'active_sessions': len(self.active_sessions),
            'session_connections': {
                session_id: len(connections) 
                for session_id, connections in self.session_connections.items()
            }
        }


# Global streaming manager instance
_global_streaming_manager = None

def get_streaming_manager() -> AudioStreamingManager:
    """Get global streaming manager instance"""
    global _global_streaming_manager
    if _global_streaming_manager is None:
        _global_streaming_manager = AudioStreamingManager()
    return _global_streaming_manager


# WebSocket Consumer for real-time audio streaming
class AudioStreamingConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time audio streaming"""
    
    async def connect(self):
        """Handle WebSocket connection"""
        try:
            # Extract connection parameters
            session_id = self.scope['url_route']['kwargs'].get('session_id')
            tenant_id = self.scope.get('tenant_id', 'default')
            
            if not session_id:
                await self.close()
                return
            
            # Accept connection
            await self.accept()
            
            # Create streaming connection
            config = StreamingConfig(
                protocol=StreamingProtocol.WEBSOCKET,
                format=StreamingFormat.JSON_WRAPPED,
                quality=StreamQuality.MEDIUM
            )
            
            connection = StreamConnection(
                connection_id=f"ws_{session_id}_{int(time.time())}",
                session_id=session_id,
                tenant_id=tenant_id,
                protocol=StreamingProtocol.WEBSOCKET,
                config=config,
                created_at=timezone.now(),
                last_activity=timezone.now(),
                websocket_consumer=self
            )
            
            # Register with streaming manager
            streaming_manager = get_streaming_manager()
            await streaming_manager.add_streaming_connection(connection)
            
            # Store connection reference
            self.connection = connection
            
            logger.info(f"WebSocket audio streaming connected: {session_id}")
            
        except Exception as e:
            logger.error(f"Error in WebSocket connect: {e}")
            await self.close()
    
    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        try:
            if hasattr(self, 'connection'):
                streaming_manager = get_streaming_manager()
                await streaming_manager.websocket_streamer.remove_connection(
                    self.connection.connection_id
                )
                
                logger.info(f"WebSocket audio streaming disconnected: {self.connection.session_id}")
                
        except Exception as e:
            logger.error(f"Error in WebSocket disconnect: {e}")
    
    async def receive(self, text_data=None, bytes_data=None):
        """Handle incoming WebSocket messages"""
        try:
            # Handle control messages (pause, resume, quality change)
            if text_data:
                data = json.loads(text_data)
                command = data.get('command')
                
                if command == 'change_quality':
                    quality = data.get('quality', 'medium')
                    await self._change_stream_quality(quality)
                elif command == 'get_stats':
                    await self._send_stats()
                    
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")
    
    async def _change_stream_quality(self, quality: str):
        """Change streaming quality"""
        try:
            if hasattr(self, 'connection'):
                quality_enum = StreamQuality(quality)
                self.connection.config.quality = quality_enum
                
                # Update quality-based settings
                if quality_enum == StreamQuality.LOW:
                    self.connection.config.sample_rate = 8000
                    self.connection.config.bit_depth = 8
                elif quality_enum == StreamQuality.HIGH:
                    self.connection.config.sample_rate = 44100
                    self.connection.config.bit_depth = 16
                    
        except Exception as e:
            logger.error(f"Error changing stream quality: {e}")
    
    async def _send_stats(self):
        """Send streaming statistics"""
        try:
            streaming_manager = get_streaming_manager()
            stats = streaming_manager.get_comprehensive_stats()
            
            await self.send(text_data=json.dumps({
                'type': 'stats',
                'data': stats
            }))
            
        except Exception as e:
            logger.error(f"Error sending stats: {e}")


# Convenience functions
async def start_audio_streaming(session_id: str) -> bool:
    """Start audio streaming for session"""
    streaming_manager = get_streaming_manager()
    return await streaming_manager.register_session(session_id)


async def stop_audio_streaming(session_id: str) -> bool:
    """Stop audio streaming for session"""
    streaming_manager = get_streaming_manager()
    return await streaming_manager.unregister_session(session_id)


async def stream_audio_to_clients(session_id: str, frame: AudioFrame) -> bool:
    """Stream audio frame to all connected clients"""
    streaming_manager = get_streaming_manager()
    return await streaming_manager.stream_audio_frame(session_id, frame)
