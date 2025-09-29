"""
Custom RTP Server for real-time audio streaming

This module implements a high-performance RTP server using asyncio for zero-loss
audio capture and real-time call management. Features include:
- Asyncio UDP server for RTP packet reception
- RTP header parsing and validation
- Session-aware packet processing
- Port management for tenant isolation
- Audio codec support (μ-law, A-law, etc.)
- Real-time audio streaming capabilities
"""

import asyncio
import logging
import struct
import socket
import time
from typing import Dict, List, Optional, Any, Callable, Tuple, Coroutine
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings
from scipy.signal import resample_poly
import collections
import numpy as np
import webrtcvad
import pyrnnoise


from core.models import Tenant
from core.session.manager import get_session_manager

logger = logging.getLogger(__name__)


@dataclass
class RTPHeader:
    """RTP packet header structure"""
    version: int = 2
    padding: bool = False
    extension: bool = False
    csrc_count: int = 0
    marker: bool = False
    payload_type: int = 0
    sequence_number: int = 0
    timestamp: int = 0
    ssrc: int = 0
    csrc_list: List[int] = field(default_factory=list)


@dataclass
class RTPPacket:
    """Complete RTP packet structure"""
    header: RTPHeader
    payload: bytes
    received_time: datetime
    local_port: int
    source_address: Tuple[str, int]
    size: int


@dataclass
class RTPEndpoint:
    """RTP endpoint configuration for a session"""
    session_id: str
    tenant_id: int
    local_port: int
    remote_host: str
    remote_port: int
    codec: str = "ulaw"
    sample_rate: int = 16000
    channels: int = 1
    active: bool = True
    created_at: datetime = field(default_factory=timezone.now)
    last_packet_time: Optional[datetime] = None
    packet_count: int = 0
    byte_count: int = 0


@dataclass
class AudioFrame:
    """Processed audio frame ready for streaming"""
    payload: bytes
    timestamp: int
    sequence_number: int
    codec: str
    sample_rate: int
    processed_time: datetime
    channels: int
    session_id: str
    original_packet: Optional[RTPPacket] = None
    is_speech:bool = False
    avg_speech_prob: float = 0.0


@dataclass
class AudioBufferStats:
    """Audio buffer statistics"""
    packets_buffered: int = 0
    packets_dropped: int = 0
    packets_played: int = 0
    jitter_ms: float = 0.0

    def to_dict(self):
        return asdict(self)

@dataclass
class AudioProcessorStats:
    """Audio processor statistics"""
    frames_processed: int = 0
    total_bytes_processed: int = 0
    conversion_errors: int = 0
    vad_voiced_frames: int = 0
    vad_silence_frames: int = 0
    rnnoise_avg_speech_prob: float = 0.0

    def to_dict(self):
        return asdict(self)

@dataclass
class DenoiserData:
    """Denoiser data structure"""
    denoised_bytes: bytes
    avg_speech_prob: float = 0.0
    is_speech:bool = False

    def __iter__(self):
        return iter(asdict(self).items())


@dataclass
class RTPServerStats:
    total_packets: int = 0
    total_bytes: int = 0
    active_endpoints: int = 0
    server_start_time: Optional[datetime] = None

    def to_dict(self):
        return asdict(self)

class AdaptiveAudioBuffer:
    """
    Adaptive jitter buffer using asyncio.Queue.
    - Buffers RTP packets based on sequence number
    - Maintains adaptive playout delay based on observed jitter
    - Pushes ready packets into an asyncio.Queue for downstream consumption
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        target_delay_ms: int = 50,
        max_delay_ms: int = 200,
        buffer_size: int = 50,
        late_threshold: int = 10,
    ):
        self.last_transit = None
        self.queue = queue
        self.buffer_size = buffer_size
        self.late_threshold = late_threshold

        # Adaptive delay parameters
        self.target_delay_ms = target_delay_ms
        self.max_delay_ms = max_delay_ms
        self.playout_delay_ms = target_delay_ms

        # Packet storage
        self.packets: Dict[int, RTPPacket] = {}
        self.arrival_times: Dict[int, float] = {}

        # Stats
        self.stats = AudioBufferStats()

        self.last_seq: Optional[int] = None
        self.base_time = time.monotonic()

    # ---- RTP packet management ----
    def add_packet(self, packet: RTPPacket) -> None:
        """
        Add a new RTP packet to the buffer while handling duplicates, late packets,
        and buffer overflow conditions. Also updates jitter statistics and attempts
        to release packets that are ready for processing in the correct sequence.

        :param packet: The RTPPacket to add to the buffer.
        :type packet: RTPPacket
        :return: None
        """
        now = time.monotonic()
        seq = packet.header.sequence_number

        if seq in self.packets:
            return  # Duplicate

        # Drop very late packets
        if self.last_seq is not None and self._seq_diff(seq, self.last_seq) < -self.late_threshold:
            self.stats.packets_dropped += 1
            return

        # Buffer overflow protection
        if len(self.packets) >= self.buffer_size:
            # Drop the packet furthest in time, not just min()
            oldest_seq = min(self.packets.keys(),
                             key=lambda s: self._seq_diff(s, self.last_seq or s))
            self.packets.pop(oldest_seq, None)
            self.arrival_times.pop(oldest_seq, None)
            self.stats.packets_dropped += 1

        self.packets[seq] = packet
        self.arrival_times[seq] = now
        self.stats.packets_buffered += 1
        self._update_jitter(arrival_time=now, rtp_timestamp=packet.header.timestamp)

        # Attempt to release packets in order
        self._release_ready_packets(now)

    def _release_ready_packets(self, now: float) -> None:
        """
        Release packets that are older than current adaptive playout delay.
        """
        for seq in sorted(self.packets.keys()):
            if (now - self.arrival_times[seq]) * 1000 >= self.playout_delay_ms:
                pkt = self.packets.pop(seq)
                self.arrival_times.pop(seq, None)
                self.last_seq = seq
                self.stats.packets_played += 1
                # Push to consumer queue (non-blocking)
                try:
                    self.queue.put_nowait(pkt)
                except asyncio.QueueFull:
                    self.stats.packets_dropped += 1
            else:
                break  # Stop once we reach a packet that is too fresh

    @staticmethod
    def _seq_diff(a: int, b: int) -> int:
        """Sequence number difference with wraparound."""
        return ((a - b + 32768) % 65536) - 32768

    def _update_jitter(self, arrival_time: float, rtp_timestamp: int, clock_rate: int = 8000) -> None:
        """
        Updates the jitter calculation based on the arrival time, RTP timestamp, and the clock rate
        of the media. This method estimates the inter-arrival jitter for RTP packets following
        the guidelines specified in RFC 3550. It also optionally adjusts the playout delay
        dynamically based on calculated jitter and other parameters.

        :param arrival_time: The time (in seconds) the packet arrived at the receiver.
        :param rtp_timestamp: The RTP timestamp from the packet which corresponds to the
            sampling instant of the first octet in the RTP payload.
        :param clock_rate: The clock rate (in Hz) used by the media. Defaults to 8000.

        :return: None
        """
        if self.last_seq is None or self.last_transit is None:
            # First packet—initialize
            self.last_transit = arrival_time - (rtp_timestamp / clock_rate)
            self.stats.jitter_ms = 0.0
            return

        transit = arrival_time - (rtp_timestamp / clock_rate)
        d = abs(transit - self.last_transit) * 1000.0  # convert to ms
        self.last_transit = transit

        # RFC 3550 smoothing
        # Fast attack, slow decay
        if d > self.stats.jitter_ms:
            alpha = 0.25  # attack
        else:
            alpha = 0.0625  # decay
        self.stats.jitter_ms += (d - self.stats.jitter_ms) * alpha

        # Optional: adapt playout delay
        new_delay = self.target_delay_ms + min(self.stats.jitter_ms, self.max_delay_ms)
        self.playout_delay_ms = 0.9 * self.playout_delay_ms + 0.1 * new_delay

    def get_stats(self) -> Dict:
        return self.stats.to_dict()


class AudioBuffer:
    """
    Jitter buffer for audio packet ordering and smoothing
    
    Handles:
    - Packet reordering based on sequence numbers
    - Jitter compensation and smoothing
    - Late packet handling
    - Buffer overflow protection
    """

    def __init__(self, buffer_size: int = 50, max_delay_ms: int = 200):
        self.buffer_size = buffer_size
        self.max_delay_ms = max_delay_ms
        self.packets: Dict[int, RTPPacket] = {}  # seq_num -> packet
        self.expected_seq = None
        self.last_processed_seq = -1
        self.buffer_start_time = None
        self.stats = {
            'packets_buffered': 0,
            'packets_dropped': 0,
            'packets_reordered': 0,
            'buffer_overflows': 0
        }

    def add_packet(self, packet: RTPPacket) -> bool:
        """Add packet to jitter buffer"""
        try:
            seq_num = packet.header.sequence_number

            # Handle sequence number wraparound
            if self.expected_seq is None:
                self.expected_seq = seq_num

            # Check if packet is too late
            if self._is_packet_too_late(seq_num):
                self.stats['packets_dropped'] += 1
                logger.debug(f"Dropped late packet: seq={seq_num}")
                return False

            # Check for buffer overflow
            if len(self.packets) >= self.buffer_size:
                self._handle_buffer_overflow()

            # Store packet
            self.packets[seq_num] = packet
            self.stats['packets_buffered'] += 1

            # Track reordered packets
            if seq_num < self.expected_seq:
                self.stats['packets_reordered'] += 1

            return True

        except Exception as e:
            logger.error(f"Error adding packet to buffer: {e}")
            return False

    def get_next_packets(self, max_packets: int = 10) -> List[RTPPacket]:
        """Get next ready packets from buffer in sequence order"""
        try:
            ready_packets = []

            # Start buffering timing if first packet
            if self.buffer_start_time is None and self.packets:
                self.buffer_start_time = time.monotonic()

            # Check if we should start processing (minimum buffering delay)
            if not self._is_ready_to_process():
                return ready_packets

            # Get consecutive packets starting from expected sequence
            current_seq = self.expected_seq
            packets_retrieved = 0

            while packets_retrieved < max_packets:
                if current_seq in self.packets:
                    packet = self.packets.pop(current_seq)
                    ready_packets.append(packet)
                    self.last_processed_seq = current_seq
                    packets_retrieved += 1
                    current_seq = (current_seq + 1) % 65536  # Handle wraparound
                else:
                    # Gap in sequence - stop processing
                    break

            # Update expected sequence
            if ready_packets:
                self.expected_seq = (self.last_processed_seq + 1) % 65536

            return ready_packets

        except Exception as e:
            logger.error(f"Error getting packets from buffer: {e}")
            return []

    def _is_packet_too_late(self, seq_num: int) -> bool:
        """Check if packet arrived too late to be useful"""
        if self.last_processed_seq == -1:
            return False

        # Calculate sequence difference handling wraparound
        seq_diff = (seq_num - self.last_processed_seq) % 65536
        if seq_diff > 32768:  # Negative difference (packet is late)
            seq_diff = seq_diff - 65536

        return seq_diff < -10  # Allow up to 10 packets behind

    def _handle_buffer_overflow(self):
        """Handle buffer overflow by dropping oldest packets"""
        if not self.packets:
            return

        # Drop oldest packets (lowest sequence numbers)
        sorted_seqs = sorted(self.packets.keys())
        packets_to_drop = len(sorted_seqs) // 4  # Drop 25% of buffer

        for seq in sorted_seqs[:packets_to_drop]:
            self.packets.pop(seq, None)

        self.stats['buffer_overflows'] += 1
        logger.warning(f"Buffer overflow: dropped {packets_to_drop} packets")

    def _is_ready_to_process(self) -> bool:
        """Check if buffer has enough packets/time to start processing"""
        if not self.packets or self.buffer_start_time is None:
            return False

        # Wait for minimum buffer time
        elapsed_ms = (time.monotonic() - self.buffer_start_time).total_seconds() * 1000
        return elapsed_ms >= (self.max_delay_ms / 4)  # Start at 25% of max delay

    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics"""
        return {
            **self.stats,
            'current_buffer_size': len(self.packets),
            'expected_seq': self.expected_seq,
            'last_processed_seq': self.last_processed_seq
        }


class AudioProcessor:
    """
    Audio processing engine for format conversion and streaming
    
    Handles:
    - Audio format conversion between codecs
    - Sample rate conversion
    - Audio level normalization
    - Real-time streaming preparation
    """

    def __init__(self):
        # RNNoise for denoising
        self.rnnoise = pyrnnoise.RNNoise(sample_rate=48000)

        # WebRTC VAD (aggressiveness 0–3; 3 = most aggressive)
        self.vad = webrtcvad.Vad(0)

        self.conversion_cache: Dict[str, bytes] = {}

        # Stats
        self.stats = AudioProcessorStats()

    async def process_packet(self, packet: RTPPacket, endpoint: RTPEndpoint, target_codec: str = None) -> Optional[AudioFrame]:
        """Process RTP packet into audio frame"""
        try:
            # Get codec information
            codec_info = AudioCodec.get_codec_info(packet.header.payload_type)
            if not codec_info:
                logger.warning(f"Unknown codec for payload type {packet.header.payload_type}")
                return None

            source_codec = codec_info['name']
            target_codec = target_codec or source_codec

            # Convert audio format if needed
            processed_payload = await self._convert_audio_format(
                packet.payload, source_codec, target_codec
            )

            if not processed_payload:
                logger.warning(f"Failed to convert payload type {packet.header.payload_type}")
                return None

            #Normalize audio level
            payload = self.normalize_audio_level(payload=processed_payload, target_level=0.1)

            #Denoise and VAD
            data = await self._denoise_payload(payload=payload)

            if not data:
                logger.warning(f"Failed to denoise payload type {packet.header.payload_type}")
                return None

            if not data.denoised_bytes:
                logger.warning("Denoised bytes is empty or None")
                return None

            # Create AudioFrame for downstream processing
            frame = AudioFrame(
                sequence_number=packet.header.sequence_number,
                timestamp=packet.header.timestamp,
                payload=data.denoised_bytes,
                codec=target_codec,
                processed_time=timezone.now(),
                sample_rate=endpoint.sample_rate,
                channels=endpoint.channels,
                session_id=endpoint.session_id,
                original_packet=packet,
                is_speech=data.is_speech,
                avg_speech_prob=data.avg_speech_prob,
            )

            # General stats
            self.stats.frames_processed += 1
            self.stats.total_bytes_processed += len(data.denoised_bytes)

            return frame
        except Exception as e:
            logger.error(f"Error processing audio packet: {e}")
            self.stats.conversion_errors += 1

            return None


    async def _denoise_payload(self, payload: bytes) ->  Optional[DenoiserData]:
        """
        Processes the given audio payload by denoising and analyzing it. The function
        utilizes RNNoise for noise reduction, calculates the speech probability for each
        frame, and performs voice activity detection (VAD) using WebRTC.

        The payload is first converted from bytes into a 16-bit PCM audio format and
        upsampled to 48 kHz for compatibility with the RNNoise library. It processes
        each frame of audio to remove noise, calculates the probability of speech for the
        frames, and adjusts audio back to a 16 kHz format for further analysis. The
        results also include whether the processed audio contains speech based on VAD output.

        If the entire processing chain completes successfully, it returns an object
        containing the denoised audio bytes, a boolean indicating speech presence, and the
        average speech probability for the processed interval.

        :param payload: Raw audio data in bytes that represents PCM audio at 16 kHz sample rate.
        :type payload: bytes
        :return: An optional object holding the denoised audio bytes, a boolean flag indicating
                 speech presence, and the average speech probability, or `None` if processing fails.
        :rtype: Optional[DenoiserData]
        """
        try:

            # Ensure bytes → int16 numpy
            pcm_16k = np.frombuffer(payload, dtype=np.int16)

            if pcm_16k.size == 0:
                logger.warning("Empty PCM16k buffer")
                return None

            # Upsample to 48 kHz for RNNoise
            pcm_48k = resample_poly(pcm_16k, up=3, down=1).astype(np.float32)
            if pcm_48k is None or pcm_48k.size == 0:
                logger.warning("Resample returned None or empty array")
                return None

            pcm_48k = pcm_48k.astype(np.float32) / 32768.0

            frame_size = 480  # 480 samples = 10 ms at 48 kHz
            denoised_frames = []
            speech_probs = []

            # Process each 10-ms frame
            for i in range(0, len(pcm_48k), frame_size):
                frame = pcm_48k[i: i + frame_size]

                if len(frame) < frame_size:
                    break  # drop incomplete trailing frame

                try:
                    for speech_prob, denoised_frame in self.rnnoise.denoise_chunk(frame):
                        denoised_frames.append(denoised_frame)
                        speech_probs.append(speech_prob)
                except Exception as e:
                    logger.error(f"Failed to denoise frame: {e}")
                    return None

            if len(denoised_frames) == 0:
                return None

            # Concatenate and convert back to int16
            denoised_48k = np.concatenate(denoised_frames)
            pcm_denoised_48k = (denoised_48k * 32768.0).astype(np.int16)

            # Downsample back to 16 kHz for VAD / pipeline
            pcm_denoised_16k = resample_poly(pcm_denoised_48k, up=1, down=3).astype(np.int16)
            if pcm_denoised_16k is None or pcm_denoised_16k.size == 0:
                logger.warning("Resample returned None or empty array")
                return None

            denoised_bytes = pcm_denoised_16k.tobytes()

            # WebRTC VAD (20 ms @ 16 kHz = 320 samples)
            vad_frame_size = 320
            is_speech = False

            if pcm_denoised_16k.size >= vad_frame_size:
                frame_bytes = pcm_denoised_16k[:vad_frame_size].tobytes()
                is_speech = self.vad.is_speech(frame_bytes, 16000)

            # Update stats
            self.stats.vad_voiced_frames = 0
            self.stats.vad_silence_frames = 0
            self.stats.rnnoise_avg_speech_prob = 0.0
            self.stats.rnnoise_frame_count = 0

            # Running average of RNNoise speech probability
            avg_prob = 0.0
            if speech_probs:
                avg_prob = float(np.mean(speech_probs))
                c = self.stats.rnnoise_frame_count
                self.stats.rnnoise_avg_speech_prob = (
                        (self.stats.rnnoise_avg_speech_prob * c + avg_prob) / (c + 1)
                )
                self.stats.rnnoise_frame_count += 1

            return DenoiserData(
                denoised_bytes=denoised_bytes,
                is_speech=is_speech,
                avg_speech_prob=avg_prob,
            )
        except Exception as e:
            logger.error(f"Error denoising payload: {e}")
            return None

    async def _convert_audio_format(self, payload: bytes, source_codec: str, target_codec: str) -> Optional[bytes]:
        """Convert audio between different formats"""
        try:
            if source_codec == target_codec:
                return payload

            # Create cache key
            cache_key = f"{source_codec}_{target_codec}_{len(payload)}"
            if cache_key in self.conversion_cache:
                return self.conversion_cache[cache_key]

            converted_payload = payload  # Start with original

            # Convert from source to linear PCM first
            if source_codec == 'ulaw':
                converted_payload = self._ulaw2lin(payload)
            elif source_codec == 'alaw':
                converted_payload = self._alaw2lin(payload)
            elif source_codec == 'slin16':
                converted_payload = self._slin16_to_lin(payload)
            elif source_codec == 'gsm':
                # GSM conversion would need external library
                logger.warning("GSM codec conversion not implemented")
                return payload

            # Convert from linear PCM to target format
            if target_codec == 'ulaw' and source_codec != 'ulaw':
                converted_payload = self._lin2ulaw(converted_payload)
            elif target_codec == 'alaw' and source_codec != 'alaw':
                converted_payload = self._lin2alaw(converted_payload)
            elif target_codec == 'slin16' and source_codec != 'slin16':
                converted_payload = self._lin_to_slin16(converted_payload)

            # Cache result for performance (limit cache size)
            if len(self.conversion_cache) < 100:
                self.conversion_cache[cache_key] = converted_payload

            return converted_payload

        except Exception as e:
            logger.error(f"Error converting audio from {source_codec} to {target_codec}: {e}")
            return None

    def normalize_audio_level(self, payload: bytes, target_level: float = 0.8) -> bytes:
        """Normalize audio level to target amplitude"""
        try:
            # Calculate current RMS level
            current_rms = self._calculate_rms(payload)
            if current_rms == 0:
                return payload

            # Calculate scaling factor
            max_level = 32767 * target_level  # 16-bit max * target level
            scale_factor = max_level / current_rms

            # Apply scaling (with clipping protection)
            scale_factor = min(scale_factor, 4.0)  # Max 4x amplification

            return self._multiply_audio(payload, scale_factor)

        except Exception as e:
            logger.error(f"Error normalizing audio level: {e}")
            return payload

    @staticmethod
    def _ulaw2lin(payload: bytes) -> bytes:
        """Convert μ-law encoded audio to linear PCM using numpy"""
        try:
            # Convert bytes to numpy array of uint8
            ulaw_data = np.frombuffer(payload, dtype=np.uint8)

            # μ-law to linear conversion algorithm
            # Flip all bits except sign bit
            sign = (ulaw_data & 0x80) >> 7
            magnitude = ulaw_data & 0x7F

            # Decode the magnitude
            exp = (magnitude & 0x70) >> 4
            mantissa = magnitude & 0x0F

            # Calculate linear value
            linear = np.where(exp == 0,
                              (mantissa << 4) + 132,
                              ((mantissa << 4) + 132) << (exp - 1))

            # Apply sign
            linear = np.where(sign == 1, -linear, linear)

            # Convert to 16-bit signed integers
            return linear.astype(np.int16).tobytes()

        except Exception as e:
            logger.error(f"Error in ulaw2lin conversion: {e}")
            return payload

    @staticmethod
    def _alaw2lin(payload: bytes) -> bytes:
        """Convert A-law encoded audio to linear PCM using numpy"""
        try:
            # Convert bytes to numpy array of uint8
            alaw_data = np.frombuffer(payload, dtype=np.uint8)

            # A-law to linear conversion algorithm
            # Invert even bits
            alaw_data ^= 0x55

            sign = (alaw_data & 0x80) >> 7
            magnitude = alaw_data & 0x7F

            # Decode the magnitude
            exp = (magnitude & 0x70) >> 4
            mantissa = magnitude & 0x0F

            # Calculate linear value
            linear = np.where(exp == 0,
                              (mantissa << 4) + 8,
                              ((mantissa << 4) + 264) << (exp - 1))

            # Apply sign
            linear = np.where(sign == 1, -linear, linear)

            # Convert to 16-bit signed integers
            return linear.astype(np.int16).tobytes()

        except Exception as e:
            logger.error(f"Error in alaw2lin conversion: {e}")
            return payload

    @staticmethod
    def _lin2ulaw(payload: bytes) -> bytes:
        """Convert linear PCM to μ-law encoded audio using numpy"""
        try:
            # Convert bytes to numpy array of int16
            linear_data = np.frombuffer(payload, dtype=np.int16)

            # Get sign and magnitude
            sign = (linear_data < 0).astype(np.uint8) << 7
            magnitude = np.abs(linear_data).astype(np.int32)

            # Clamp to 14-bit range
            magnitude = np.minimum(magnitude, 8159)

            # Add bias
            magnitude += 132

            # Find exponent
            exp = np.zeros_like(magnitude, dtype=np.uint8)
            for i in range(7, -1, -1):
                mask = magnitude >= (256 << i)
                exp = np.where(mask & (exp == 0), i + 1, exp)

            # Calculate mantissa
            mantissa = np.where(exp == 0,
                                (magnitude >> 4) & 0x0F,
                                ((magnitude >> (exp + 3)) & 0x0F))

            # Combine components
            ulaw = sign | (exp << 4) | mantissa

            # Invert all bits
            ulaw ^= 0xFF

            return ulaw.astype(np.uint8).tobytes()

        except Exception as e:
            logger.error(f"Error in lin2ulaw conversion: {e}")
            return payload

    @staticmethod
    def _lin2alaw(payload: bytes) -> bytes:
        """Convert linear PCM to A-law encoded audio using numpy"""
        try:
            # Convert bytes to numpy array of int16
            linear_data = np.frombuffer(payload, dtype=np.int16)

            # Get sign and magnitude
            sign = (linear_data < 0).astype(np.uint8) << 7
            magnitude = np.abs(linear_data).astype(np.int32)

            # Clamp to 12-bit range
            magnitude = np.minimum(magnitude, 4095)

            # Find exponent
            exp = np.zeros_like(magnitude, dtype=np.uint8)
            for i in range(7, -1, -1):
                mask = magnitude >= (256 << i)
                exp = np.where(mask & (exp == 0), i + 1, exp)

            # Calculate mantissa
            mantissa = np.where(exp == 0,
                                (magnitude >> 4) & 0x0F,
                                ((magnitude >> (exp + 3)) & 0x0F))

            # Combine components
            alaw = sign | (exp << 4) | mantissa

            # Invert even bits
            alaw ^= 0x55

            return alaw.astype(np.uint8).tobytes()

        except Exception as e:
            logger.error(f"Error in lin2alaw conversion: {e}")
            return payload

    @staticmethod
    def _slin16_to_lin(payload: bytes) -> bytes:
        """Convert SLIN16 (16kHz, 16-bit) to linear PCM (8kHz, 16-bit) using numpy"""
        try:
            # Convert bytes to numpy array of int16
            slin16_data = np.frombuffer(payload, dtype=np.int16)
            
            # Downsample from 16kHz to 8kHz by taking every second sample
            # This is a simple decimation - more sophisticated filtering could be added
            downsampled_data = slin16_data[::2]
            
            return downsampled_data.astype(np.int16).tobytes()
            
        except Exception as e:
            logger.error(f"Error in slin16_to_lin conversion: {e}")
            return payload

    @staticmethod
    def _lin_to_slin16(payload: bytes) -> bytes:
        """Convert linear PCM (8kHz, 16-bit) to SLIN16 (16kHz, 16-bit) using numpy"""
        try:
            # Convert bytes to numpy array of int16
            linear_data = np.frombuffer(payload, dtype=np.int16)
            
            # Upsample from 8kHz to 16kHz by duplicating each sample
            # This is simple sample and hold - more sophisticated interpolation could be added
            upsampled_data = np.repeat(linear_data, 2)
            
            return upsampled_data.astype(np.int16).tobytes()
            
        except Exception as e:
            logger.error(f"Error in lin_to_slin16 conversion: {e}")
            return payload

    @staticmethod
    def _calculate_rms(payload: bytes) -> float:
        """Calculate RMS (Root Mean Square) value of audio data using numpy"""
        try:
            # Convert bytes to numpy array of int16
            audio_data = np.frombuffer(payload, dtype=np.int16)

            # Calculate RMS
            rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))

            return float(rms)

        except Exception as e:
            logger.error(f"Error calculating RMS: {e}")
            return 0.0

    @staticmethod
    def _multiply_audio(payload: bytes, scale_factor: float) -> bytes:
        """Multiply audio samples by scale factor using numpy"""
        try:
            # Convert bytes to numpy array of int16
            audio_data = np.frombuffer(payload, dtype=np.int16)

            # Apply scaling
            scaled_data = audio_data.astype(np.float64) * scale_factor

            # Clamp to int16 range to prevent overflow
            scaled_data = np.clip(scaled_data, -32768, 32767)

            # Convert back to int16
            return scaled_data.astype(np.int16).tobytes()

        except Exception as e:
            logger.error(f"Error multiplying audio: {e}")
            return payload

    def get_stats(self) -> Dict[str, Any]:
        """Get processing statistics"""
        return self.stats.to_dict()


class RTPQualityMonitor:
    """
    RTP quality monitoring and analysis
    
    Provides comprehensive quality metrics including:
    - Packet loss detection and calculation
    - Jitter analysis and statistics
    - Bandwidth utilization monitoring
    - Call quality scoring (MOS estimation)
    - Network condition assessment
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.start_time = timezone.now()
        self.last_update = timezone.now()

        # Packet tracking
        self.total_packets_expected = 0
        self.total_packets_received = 0
        self.last_sequence_number = None
        self.sequence_gaps = []

        # Jitter tracking
        self.jitter_samples = collections.deque(maxlen=1000)  # Keep last 1000 samples
        self.transit_times = collections.deque(maxlen=100)
        self.instantaneous_jitter = 0.0
        self.average_jitter = 0.0

        # Bandwidth tracking
        self.bandwidth_samples = collections.deque(maxlen=60)  # 1 minute of samples
        self.current_bandwidth = 0.0
        self.peak_bandwidth = 0.0
        self.bytes_received = 0

        # Quality metrics
        self.quality_scores = collections.deque(maxlen=100)
        self.current_mos = 0.0

        # Timing tracking
        self.packet_arrival_times = collections.deque(maxlen=100)

    def update_with_packet(self, packet: RTPPacket):
        """Update quality metrics with new packet"""
        try:
            current_time = timezone.now()
            seq_num = packet.header.sequence_number
            timestamp = packet.header.timestamp

            # Update packet counts
            self.total_packets_received += 1
            self.bytes_received += packet.size

            # Calculate expected packets (handle sequence number gaps)
            if self.last_sequence_number is not None:
                expected_next = (self.last_sequence_number + 1) % 65536
                if seq_num != expected_next:
                    gap = self._calculate_sequence_gap(expected_next, seq_num)
                    if gap > 0:
                        self.sequence_gaps.append(gap)
                        self.total_packets_expected += gap

            self.last_sequence_number = seq_num
            self.total_packets_expected += 1

            # Update jitter calculations
            self._update_jitter(packet, current_time)

            # Update bandwidth calculations
            self._update_bandwidth(packet.size, current_time)

            # Store packet arrival time
            self.packet_arrival_times.append(current_time)

            # Update quality score
            self._calculate_quality_score()

            self.last_update = current_time

        except Exception as e:
            logger.error(f"Error updating quality metrics: {e}")

    def _calculate_sequence_gap(self, expected: int, received: int) -> int:
        """Calculate gap in sequence numbers handling wraparound"""
        gap = (received - expected) % 65536
        if gap > 32768:  # Wraparound case
            gap = gap - 65536
        return max(0, gap - 1)  # -1 because we're looking at the gap, not including the received packet

    def _update_jitter(self, packet: RTPPacket, arrival_time: datetime):
        """Update jitter calculations based on RFC 3550"""
        try:
            # Convert arrival time to timestamp units (assuming 8kHz sample rate)
            arrival_timestamp = int(arrival_time.timestamp() * 8000)
            rtp_timestamp = packet.header.timestamp

            # Calculate transit time
            transit = arrival_timestamp - rtp_timestamp
            self.transit_times.append(transit)

            # Calculate jitter if we have previous transit time
            if len(self.transit_times) >= 2:
                d = abs(transit - self.transit_times[-2])

                # RFC 3550 jitter calculation
                self.instantaneous_jitter += (d - self.instantaneous_jitter) / 16.0
                self.jitter_samples.append(self.instantaneous_jitter)

                # Calculate average jitter
                if self.jitter_samples:
                    self.average_jitter = sum(self.jitter_samples) / len(self.jitter_samples)

        except Exception as e:
            logger.error(f"Error calculating jitter: {e}")

    def _update_bandwidth(self, packet_size: int, current_time: datetime):
        """Update bandwidth calculations"""
        try:
            # Calculate bandwidth over 1-second intervals
            one_second_ago = current_time - timedelta(seconds=1)

            # Add current sample
            self.bandwidth_samples.append({
                'time': current_time,
                'bytes': packet_size
            })

            # Remove old samples
            while (self.bandwidth_samples and
                   self.bandwidth_samples[0]['time'] < one_second_ago):
                self.bandwidth_samples.popleft()

            # Calculate current bandwidth (bytes per second)
            if len(self.bandwidth_samples) > 1:
                time_span = (self.bandwidth_samples[-1]['time'] -
                             self.bandwidth_samples[0]['time']).total_seconds()
                if time_span > 0:
                    total_bytes = sum(sample['bytes'] for sample in self.bandwidth_samples)
                    self.current_bandwidth = total_bytes / time_span
                    self.peak_bandwidth = max(self.peak_bandwidth, self.current_bandwidth)

        except Exception as e:
            logger.error(f"Error calculating bandwidth: {e}")

    def _calculate_quality_score(self):
        """Calculate MOS (Mean Opinion Score) estimate based on network metrics"""
        try:
            # Base MOS score
            mos = 4.5

            # Packet loss penalty
            packet_loss_rate = self.get_packet_loss_rate()
            if packet_loss_rate > 0:
                # Each 1% packet loss reduces MOS by ~0.1
                mos -= min(packet_loss_rate * 10, 2.0)

            # Jitter penalty
            if self.average_jitter > 0:
                # High jitter (>50ms) significantly impacts quality
                jitter_ms = self.average_jitter / 8.0  # Convert from timestamp units to ms
                if jitter_ms > 50:
                    mos -= min((jitter_ms - 50) / 50, 1.5)
                elif jitter_ms > 20:
                    mos -= (jitter_ms - 20) / 100

            # Ensure MOS stays in valid range
            self.current_mos = max(1.0, min(5.0, mos))
            self.quality_scores.append(self.current_mos)

        except Exception as e:
            logger.error(f"Error calculating quality score: {e}")
            self.current_mos = 0.0

    def get_packet_loss_rate(self) -> float:
        """Calculate packet loss rate as percentage"""
        if self.total_packets_expected == 0:
            return 0.0

        packets_lost = self.total_packets_expected - self.total_packets_received
        return (packets_lost / self.total_packets_expected) * 100.0

    def get_jitter_stats(self) -> Dict[str, float]:
        """Get comprehensive jitter statistics"""
        if not self.jitter_samples:
            return {
                'current_jitter_ms': 0.0,
                'average_jitter_ms': 0.0,
                'max_jitter_ms': 0.0,
                'min_jitter_ms': 0.0
            }

        # Convert from timestamp units to milliseconds (assuming 8kHz)
        current_jitter_ms = self.instantaneous_jitter / 8.0
        average_jitter_ms = self.average_jitter / 8.0
        max_jitter_ms = max(self.jitter_samples) / 8.0
        min_jitter_ms = min(self.jitter_samples) / 8.0

        return {
            'current_jitter_ms': current_jitter_ms,
            'average_jitter_ms': average_jitter_ms,
            'max_jitter_ms': max_jitter_ms,
            'min_jitter_ms': min_jitter_ms
        }

    def get_bandwidth_stats(self) -> Dict[str, float]:
        """Get bandwidth utilization statistics"""
        return {
            'current_bandwidth_bps': self.current_bandwidth,
            'peak_bandwidth_bps': self.peak_bandwidth,
            'current_bandwidth_kbps': self.current_bandwidth / 1024,
            'peak_bandwidth_kbps': self.peak_bandwidth / 1024,
            'total_bytes_received': self.bytes_received
        }

    def get_quality_report(self) -> Dict[str, Any]:
        """Generate comprehensive quality report"""
        session_duration = (self.last_update - self.start_time).total_seconds()

        # Calculate average MOS
        avg_mos = sum(self.quality_scores) / len(self.quality_scores) if self.quality_scores else 0.0

        # Determine quality classification
        if self.current_mos >= 4.0:
            quality_class = "Excellent"
        elif self.current_mos >= 3.6:
            quality_class = "Good"
        elif self.current_mos >= 3.1:
            quality_class = "Fair"
        elif self.current_mos >= 2.6:
            quality_class = "Poor"
        else:
            quality_class = "Bad"

        return {
            'session_id': self.session_id,
            'session_duration_seconds': session_duration,
            'timestamp': self.last_update.isoformat(),

            # Packet statistics
            'packets_received': self.total_packets_received,
            'packets_expected': self.total_packets_expected,
            'packets_lost': self.total_packets_expected - self.total_packets_received,
            'packet_loss_rate_percent': self.get_packet_loss_rate(),
            'sequence_gaps': len(self.sequence_gaps),

            # Jitter statistics
            'jitter': self.get_jitter_stats(),

            # Bandwidth statistics  
            'bandwidth': self.get_bandwidth_stats(),

            # Quality metrics
            'current_mos': self.current_mos,
            'average_mos': avg_mos,
            'quality_classification': quality_class,

            # Network condition assessment
            'network_condition': self._assess_network_condition()
        }

    def _assess_network_condition(self) -> str:
        """Assess overall network condition based on metrics"""
        packet_loss = self.get_packet_loss_rate()
        jitter_stats = self.get_jitter_stats()
        avg_jitter_ms = jitter_stats['average_jitter_ms']

        if packet_loss < 1.0 and avg_jitter_ms < 20:
            return "Excellent"
        elif packet_loss < 3.0 and avg_jitter_ms < 50:
            return "Good"
        elif packet_loss < 5.0 and avg_jitter_ms < 100:
            return "Fair"
        elif packet_loss < 10.0 and avg_jitter_ms < 200:
            return "Poor"
        else:
            return "Critical"


class RTPStatisticsCollector:
    """
    Centralized RTP statistics collection and reporting
    
    Aggregates statistics from multiple endpoints and provides
    system-wide monitoring and alerting capabilities.
    """

    def __init__(self):
        self.quality_monitors: Dict[str, RTPQualityMonitor] = {}  # session_id -> monitor
        self.global_stats = {
            'total_sessions': 0,
            'active_sessions': 0,
            'total_packets_processed': 0,
            'total_bytes_processed': 0,
            'average_quality_score': 0.0,
            'system_start_time': timezone.now()
        }
        self.alert_thresholds = {
            'packet_loss_rate': 5.0,  # 5% packet loss
            'jitter_ms': 100.0,  # 100ms jitter
            'quality_score': 3.0  # MOS below 3.0
        }
        self.active_alerts: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> alerts

    def create_quality_monitor(self, session_id: str) -> RTPQualityMonitor:
        """Create quality monitor for new session"""
        monitor = RTPQualityMonitor(session_id)
        self.quality_monitors[session_id] = monitor
        self.global_stats['total_sessions'] += 1
        self.global_stats['active_sessions'] += 1
        return monitor

    def remove_quality_monitor(self, session_id: str):
        """Remove quality monitor for ended session"""
        if session_id in self.quality_monitors:
            del self.quality_monitors[session_id]
            self.global_stats['active_sessions'] = len(self.quality_monitors)
            self.active_alerts.pop(session_id, None)

    def update_global_stats(self):
        """Update global statistics from all monitors"""
        try:
            if not self.quality_monitors:
                return

            total_packets = sum(m.total_packets_received for m in self.quality_monitors.values())
            total_bytes = sum(m.bytes_received for m in self.quality_monitors.values())
            quality_scores = [m.current_mos for m in self.quality_monitors.values() if m.current_mos > 0]

            self.global_stats.update({
                'active_sessions': len(self.quality_monitors),
                'total_packets_processed': total_packets,
                'total_bytes_processed': total_bytes,
                'average_quality_score': sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
            })

            # Check for system-wide alerts
            self._check_system_alerts()

        except Exception as e:
            logger.error(f"Error updating global statistics: {e}")

    def _check_system_alerts(self):
        """Check for alert conditions across all sessions"""
        try:
            for session_id, monitor in self.quality_monitors.items():
                alerts = []

                # Check packet loss threshold
                packet_loss = monitor.get_packet_loss_rate()
                if packet_loss > self.alert_thresholds['packet_loss_rate']:
                    alerts.append({
                        'type': 'high_packet_loss',
                        'severity': 'warning' if packet_loss < 10 else 'critical',
                        'value': packet_loss,
                        'threshold': self.alert_thresholds['packet_loss_rate'],
                        'timestamp': timezone.now().isoformat()
                    })

                # Check jitter threshold
                jitter_stats = monitor.get_jitter_stats()
                avg_jitter = jitter_stats['average_jitter_ms']
                if avg_jitter > self.alert_thresholds['jitter_ms']:
                    alerts.append({
                        'type': 'high_jitter',
                        'severity': 'warning' if avg_jitter < 200 else 'critical',
                        'value': avg_jitter,
                        'threshold': self.alert_thresholds['jitter_ms'],
                        'timestamp': timezone.now().isoformat()
                    })

                # Check quality score threshold
                if monitor.current_mos < self.alert_thresholds['quality_score']:
                    alerts.append({
                        'type': 'poor_quality',
                        'severity': 'warning' if monitor.current_mos > 2.5 else 'critical',
                        'value': monitor.current_mos,
                        'threshold': self.alert_thresholds['quality_score'],
                        'timestamp': timezone.now().isoformat()
                    })

                # Store alerts
                if alerts:
                    self.active_alerts[session_id] = alerts
                    logger.warning(f"Quality alerts for session {session_id}: {len(alerts)} issues detected")
                else:
                    self.active_alerts.pop(session_id, None)

        except Exception as e:
            logger.error(f"Error checking system alerts: {e}")

    def get_system_report(self) -> Dict[str, Any]:
        """Generate comprehensive system statistics report"""
        uptime = (timezone.now() - self.global_stats['system_start_time']).total_seconds()

        # Calculate per-session averages
        if self.quality_monitors:
            avg_packet_loss = sum(m.get_packet_loss_rate() for m in self.quality_monitors.values()) / len(
                self.quality_monitors)
            avg_jitter = sum(m.get_jitter_stats()['average_jitter_ms'] for m in self.quality_monitors.values()) / len(
                self.quality_monitors)
        else:
            avg_packet_loss = 0.0
            avg_jitter = 0.0

        return {
            'timestamp': timezone.now().isoformat(),
            'system_uptime_seconds': uptime,
            'global_stats': self.global_stats,
            'session_stats': {
                'total_sessions': self.global_stats['total_sessions'],
                'active_sessions': len(self.quality_monitors),
                'average_packet_loss_rate': avg_packet_loss,
                'average_jitter_ms': avg_jitter,
                'average_quality_score': self.global_stats['average_quality_score']
            },
            'active_alerts_count': len(self.active_alerts),
            'sessions_with_alerts': list(self.active_alerts.keys())
        }

    def get_session_report(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed report for specific session"""
        monitor = self.quality_monitors.get(session_id)
        if not monitor:
            return None

        report = monitor.get_quality_report()

        # Add alert information
        alerts = self.active_alerts.get(session_id, [])
        report['active_alerts'] = alerts
        report['alert_count'] = len(alerts)

        return report


class AudioCodec:
    """Audio codec definitions and conversion utilities"""

    CODECS = {
        'ulaw': {'payload_type': 0, 'sample_rate': 8000, 'channels': 1},
        'alaw': {'payload_type': 8, 'sample_rate': 8000, 'channels': 1},
        'gsm': {'payload_type': 3, 'sample_rate': 8000, 'channels': 1},
        'g722': {'payload_type': 9, 'sample_rate': 16000, 'channels': 1},
        'l16': {'payload_type': 10, 'sample_rate': 44100, 'channels': 2},
        'slin16' : {'payload_type': 118, 'sample_rate': 16000, 'channels': 1}
    }

    @classmethod
    def get_codec_info(cls, payload_type: int) -> Optional[Dict[str, Any]]:
        """Get codec information by payload type"""
        for codec_name, info in cls.CODECS.items():
            if info['payload_type'] == payload_type:
                return {'name': codec_name, **info}
        return None

    @classmethod
    def validate_codec(cls, codec_name: str) -> bool:
        """Validate if codec is supported"""
        return codec_name.lower() in cls.CODECS


class RTPServerProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for RTP packets"""

    def __init__(self, rtp_server):
        packet_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.audio_buffer = AdaptiveAudioBuffer(queue=packet_queue)
        self.local_port = 0
        self.rtp_server = rtp_server
        self.transport = None

    def connection_made(self, transport):
        """Called when UDP socket is ready"""
        self.transport = transport
        sock = transport.get_extra_info('socket')
        if sock:
            self.local_port = sock.getsockname()[1]
            logger.info(f"RTP server listening on {sock.getsockname()}")
        asyncio.create_task(self._consumer_task())

    def connection_lost(self, exc):
        """Called when RTP socket is closed"""
        self.local_port = 0
        asyncio.create_task(self.audio_buffer.queue.put(None))

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming RTP packets"""
        try:
            # Parse RTP packet
            packet = self._parse_rtp_packet(data, addr)
            if packet:
                # asyncio.create_task(self.rtp_server.process_rtp_packet(packet))
                self.audio_buffer.add_packet(packet)
        except Exception as e:
            logger.error(f"Error processing RTP packet from {addr}: {e}")


    async def _consumer_task(self):
        """
        Consumes packets from the given queue and processes them asynchronously.

        This coroutine continuously retrieves packets from the provided async queue
        as long as the `active` attribute is set to True. Each packet is then
        processed by creating a new task to handle it with the `process_rtp_packet`
        method of the `rtp_server`.

        :return: None
        """

        try:
            while True:
                packet = await self.audio_buffer.queue.get()
                if packet is None:  # sentinel for shutdown
                    break
                await self.rtp_server.process_rtp_packet(packet)
                self.audio_buffer.queue.task_done()
        except asyncio.CancelledError:
            logger.debug("RTP server consumer task cancelled")
            pass


    def _parse_rtp_packet(self, data: bytes, addr: Tuple[str, int]) -> Optional[RTPPacket]:
        """Parse raw UDP data into RTP packet structure"""
        try:

            if len(data) < 12:  # Minimum RTP header size
                logger.warning(f"RTP packet too small from {addr}: {len(data)} bytes")
                return None

            # Parse fixed header (12 bytes)
            header_data = struct.unpack('!BBHII', data[:12])

            # Extract header fields
            version_flags = header_data[0]
            version = (version_flags >> 6) & 0x3
            padding = bool((version_flags >> 5) & 0x1)
            extension = bool((version_flags >> 4) & 0x1)
            csrc_count = version_flags & 0xF

            marker_pt = header_data[1]
            marker = bool((marker_pt >> 7) & 0x1)
            payload_type = marker_pt & 0x7F

            sequence_number = header_data[2]
            timestamp = header_data[3]
            ssrc = header_data[4]

            # Validate RTP version
            if version != 2:
                logger.warning(f"Invalid RTP version {version} from {addr}")
                return None

            # Parse CSRC list if present
            header_size = 12
            csrc_list = []
            if csrc_count > 0:
                csrc_end = 12 + (csrc_count * 4)
                if len(data) < csrc_end:
                    logger.warning(f"Invalid CSRC count {csrc_count} from {addr}")
                    return None

                for i in range(csrc_count):
                    csrc_start = 12 + (i * 4)
                    csrc = struct.unpack('!I', data[csrc_start:csrc_start + 4])[0]
                    csrc_list.append(csrc)
                header_size = csrc_end

            # Handle extension header if present
            if extension:
                if len(data) < header_size + 4:
                    logger.warning(f"Invalid extension header from {addr}")
                    return None

                ext_header = struct.unpack('!HH', data[header_size:header_size + 4])
                ext_length = ext_header[1] * 4
                header_size += 4 + ext_length

            # Extract payload
            payload = data[header_size:]

            # Create RTP header object
            header = RTPHeader(
                version=version,
                padding=padding,
                extension=extension,
                csrc_count=csrc_count,
                marker=marker,
                payload_type=payload_type,
                sequence_number=sequence_number,
                timestamp=timestamp,
                ssrc=ssrc,
                csrc_list=csrc_list
            )

            # Create RTP packet object
            packet = RTPPacket(
                header=header,
                payload=payload,
                received_time=timezone.now(),
                local_port=self.local_port,
                source_address=addr,
                size=len(data)
            )

            return packet

        except Exception as e:
            logger.error(f"Error parsing RTP packet from {addr}: {e}")
            return None


class RTPServer:
    """
    Custom RTP server for real-time audio processing
    
    Provides:
    - Multi-tenant port isolation
    - Session-aware packet processing
    - Real-time audio streaming
    - Codec support and conversion
    - Performance monitoring
    """

    def __init__(self):
        self.endpoints: Dict[int, RTPEndpoint] = {}  # port -> endpoint
        self.session_endpoints: Dict[str, RTPEndpoint] = {}  # session_id -> endpoint
        self.servers: Dict[int, Tuple[asyncio.DatagramTransport, RTPServerProtocol]] = {}
        self.session_manager = get_session_manager()
        self.packet_handlers: Dict[str, any] = {}

        self.statistics = RTPServerStats()
        self._lock = asyncio.Lock()
        logger.info("RTP Server initialized")

    async def create_endpoint(self, session_id: str, tenant_id: int,
                              remote_host: str, remote_port: int,
                              codec: str = "ulaw") -> Optional[RTPEndpoint]:
        """Create a new RTP endpoint for a session"""
        try:

            async with self._lock:
                # Validate codec
                if not AudioCodec.validate_codec(codec):
                    logger.error(f"Unsupported codec: {codec}")
                    return None

                # Get available port for tenant
                local_port = await self._get_available_port(tenant_id)
                if not local_port:
                    logger.error(f"No available ports for tenant {tenant_id}")
                    return None

                # Create endpoint
                endpoint = RTPEndpoint(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    local_port=local_port,
                    remote_host=remote_host,
                    remote_port=remote_port,
                    codec=codec
                )

                # Start UDP server for this endpoint
                success = await self._start_server_for_port(local_port)
                if not success:
                    logger.error(f"Failed to start server on port {local_port}")
                    return None

                # Register endpoint
                self.endpoints[local_port] = endpoint
                self.session_endpoints[session_id] = endpoint
                self.statistics.active_endpoints += 1

                logger.info(f"Created RTP endpoint for session {session_id} on port {local_port}")
                return endpoint

        except Exception as e:
            logger.error(f"Error creating RTP endpoint: {e}")
            return None

    async def _get_available_port(self, tenant_id: int) -> Optional[int]:
        """Get an available port within tenant's range"""
        try:
            # Get tenant configuration
            tenant = await Tenant.objects.aget(id=tenant_id)
            port_start = tenant.rtp_port_range_start
            port_end = tenant.rtp_port_range_end

            # Find available port
            for port in range(port_start, port_end + 1):
                if port not in self.endpoints and port not in self.servers:
                    # Test if port is actually available
                    try:
                        test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        test_sock.bind(('', port))
                        test_sock.close()
                        return port
                    except OSError:
                        continue  # Port in use

            logger.warning(f"No available ports in range {port_start}-{port_end} for tenant {tenant_id}")
            return None

        except Exception as e:
            logger.error(f"Error finding available port for tenant {tenant_id}: {e}")
            return None

    async def _start_server_for_port(self, port: int) -> bool:
        """Start UDP server for specific port"""
        try:
            loop = asyncio.get_event_loop()

            # Create protocol and transport
            protocol = RTPServerProtocol(self)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=('0.0.0.0', port)
            )

            # Store server reference
            self.servers[port] = (transport, protocol)

            logger.info(f"Started RTP server on port {port}")
            return True

        except Exception as e:
            logger.error(f"Error starting RTP server on port {port}: {e}")
            return False

    async def process_rtp_packet(self, packet: RTPPacket):
        """Process incoming RTP packet"""
        try:
            # Find endpoint by source port (assuming standard port mapping)
            source_address = packet.source_address[0]
            local_port = int(packet.local_port)
            endpoint = None

            if local_port in self.endpoints and self.endpoints[local_port].remote_host == source_address:
                endpoint = self.endpoints[local_port]

            if not endpoint:
                logger.debug(f"No available ports for tenant {local_port}")
                logger.debug(f"No endpoint found for packet from {source_address}")
                return

            # Update endpoint statistics
            endpoint.packet_count += 1
            endpoint.byte_count += packet.size
            endpoint.last_packet_time = packet.received_time

            # Update global statistics
            self.statistics.total_packets += 1
            self.statistics.total_bytes += packet.size

            # Validate codec
            codec_info = AudioCodec.get_codec_info(packet.header.payload_type)
            if not codec_info:
                logger.warning(f"Unknown codec payload type: {packet.header.payload_type}")
                return

            # Update codec, sample rate and channels according to the packet
            endpoint.codec = codec_info['name']
            endpoint.sample_rate = codec_info['sample_rate']
            endpoint.channels = codec_info['channels']

            # Call registered packet handlers
            for handler in self.packet_handlers[endpoint.session_id]:
                try:
                    await handler(packet, endpoint)
                except Exception as e:
                    logger.error(f"Error in packet handler: {e}")

            # logger.debug(f"Processed RTP packet: session={endpoint.session_id}, "
            #              f"seq={packet.header.sequence_number}, "
            #              f"payload={len(packet.payload)} bytes")

        except Exception as e:
            logger.error(f"Error processing RTP packet: {e}")

    async def remove_endpoint(self, session_id: str):
        """Remove RTP endpoint and cleanup resources"""
        try:
            async with self._lock:
                endpoint = self.session_endpoints.get(session_id)
                if not endpoint:
                    logger.warning(f"No endpoint found for session {session_id}")
                    return

                port = endpoint.local_port

                # Stop server for this port
                if port in self.servers:
                    transport, protocol = self.servers[port]
                    transport.close()
                    del self.servers[port]

                # Remove endpoint references
                if port in self.endpoints:
                    del self.endpoints[port]
                if session_id in self.session_endpoints:
                    del self.session_endpoints[session_id]

                self.statistics.active_endpoints -= 1

                logger.info(f"Removed RTP endpoint for session {session_id} on port {port}")

        except Exception as e:
            logger.error(f"Error removing RTP endpoint: {e}")

    def register_packet_handler(self, session_id, handler: Callable[[RTPPacket, RTPEndpoint], None]):
        """Register a handler for processed RTP packets"""
        if session_id not in self.packet_handlers:
            self.packet_handlers[session_id] = [] #List[Callable[[RTPPacket, RTPEndpoint], None]]
        self.packet_handlers[session_id].append(handler)
        logger.info(f"Registered RTP packet handler: {handler.__name__}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get server statistics"""
        stats = self.statistics.to_dict().copy()
        stats['active_endpoints'] = len(self.endpoints)
        if stats['server_start_time']:
            uptime = (timezone.now() - stats['server_start_time']).total_seconds()
            stats['uptime_seconds'] = uptime
        return stats

    async def start(self):
        """Start the RTP server"""
        try:
            self.statistics.server_start_time = timezone.now()
            logger.info("RTP Server started successfully")

        except Exception as e:
            logger.error(f"Error starting RTP server: {e}")
            raise

    async def stop(self):
        """Stop the RTP server and cleanup all resources"""
        try:
            async with self._lock:
                # Close all servers
                for port, (transport, protocol) in self.servers.items():
                    transport.close()

                # Clear all data
                self.servers.clear()
                self.endpoints.clear()
                self.session_endpoints.clear()
                self.packet_handlers.clear()

                logger.info("RTP Server stopped")

        except Exception as e:
            logger.error(f"Error stopping RTP server: {e}")


# Global RTP server instance
_rtp_server = None


def get_rtp_server() -> RTPServer:
    """Get global RTP server instance"""
    global _rtp_server
    if _rtp_server is None:
        _rtp_server = RTPServer()
    return _rtp_server


class RTPSessionEndpointManager:
    """
    Advanced RTP session endpoint management
    
    Provides comprehensive endpoint lifecycle management including:
    - Session creation and destruction
    - Health monitoring and timeout handling
    - Dynamic port allocation and cleanup
    - Endpoint state synchronization
    """

    def __init__(self, rtp_server: RTPServer):
        self.rtp_server = rtp_server
        self.endpoint_states: Dict[str, str] = {}  # session_id -> state
        self.endpoint_timeouts: Dict[str, datetime] = {}  # session_id -> timeout
        self.cleanup_task: Optional[asyncio.Task] = None
        self.health_check_task: Optional[asyncio.Task] = None
        self.session_manager = get_session_manager()

        # Configuration
        self.endpoint_timeout = getattr(settings, 'RTP_ENDPOINT_TIMEOUT', 300)  # 5 minutes
        self.cleanup_interval = getattr(settings, 'RTP_CLEANUP_INTERVAL', 60)  # 1 minute
        self.health_check_interval = getattr(settings, 'RTP_HEALTH_CHECK_INTERVAL', 30)  # 30 seconds

        logger.info("RTP Session Endpoint Manager initialized")

    async def start(self):
        """Start endpoint management tasks"""
        try:
            # Start periodic cleanup task
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())

            # Start health check task
            self.health_check_task = asyncio.create_task(self._health_check_loop())

            logger.info("RTP endpoint management tasks started")

        except Exception as e:
            logger.error(f"Error starting RTP endpoint management: {e}")
            raise

    async def stop(self):
        """Stop endpoint management tasks"""
        try:
            if self.cleanup_task:
                self.cleanup_task.cancel()
            if self.health_check_task:
                self.health_check_task.cancel()

            # Wait for tasks to complete
            tasks = [t for t in [self.cleanup_task, self.health_check_task] if t and not t.cancelled()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            logger.info("RTP endpoint management stopped")

        except Exception as e:
            logger.error(f"Error stopping RTP endpoint management: {e}")

    async def create_session_endpoint(self, session_id: str, tenant_id: int,
                                      remote_host: str, remote_port: int,
                                      codec: str = "ulaw") -> Optional[RTPEndpoint]:
        """Create a new RTP endpoint with full session management"""
        try:
            # Create endpoint through RTP server
            endpoint = await self.rtp_server.create_endpoint(
                session_id, tenant_id, remote_host, remote_port, codec
            )

            if endpoint:
                # Set initial state
                self.endpoint_states[session_id] = "created"
                self.endpoint_timeouts[session_id] = timezone.now() + timedelta(seconds=self.endpoint_timeout)

                # Update session with endpoint information
                await self._update_session_endpoint_info(session_id, endpoint)

                logger.info(f"Created session endpoint for {session_id} on port {endpoint.local_port}")
                return endpoint

            return None

        except Exception as e:
            logger.error(f"Error creating session endpoint: {e}")
            return None

    async def activate_endpoint(self, session_id: str):
        """Activate an endpoint (mark as receiving traffic)"""
        try:
            if session_id in self.endpoint_states:
                self.endpoint_states[session_id] = "active"
                # Extend timeout for active endpoints
                self.endpoint_timeouts[session_id] = timezone.now() + timedelta(seconds=self.endpoint_timeout * 2)
                logger.debug(f"Activated endpoint for session {session_id}")

        except Exception as e:
            logger.error(f"Error activating endpoint for session {session_id}: {e}")

    async def deactivate_endpoint(self, session_id: str):
        """Deactivate an endpoint (mark as idle)"""
        try:
            if session_id in self.endpoint_states:
                self.endpoint_states[session_id] = "idle"
                # Shorter timeout for idle endpoints
                self.endpoint_timeouts[session_id] = timezone.now() + timedelta(seconds=60)
                logger.debug(f"Deactivated endpoint for session {session_id}")

        except Exception as e:
            logger.error(f"Error deactivating endpoint for session {session_id}: {e}")

    async def destroy_session_endpoint(self, session_id: str):
        """Destroy a session endpoint and cleanup resources"""
        try:
            # Remove endpoint from RTP server
            await self.rtp_server.remove_endpoint(session_id)

            # Clean up management state
            self.endpoint_states.pop(session_id, None)
            self.endpoint_timeouts.pop(session_id, None)

            # Update session to remove endpoint info
            await self._clear_session_endpoint_info(session_id)

            logger.info(f"Destroyed session endpoint for {session_id}")

        except Exception as e:
            logger.error(f"Error destroying session endpoint: {e}")

    async def get_endpoint_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get comprehensive endpoint information"""
        try:
            endpoint = self.rtp_server.session_endpoints.get(session_id)
            if not endpoint:
                return None

            state = self.endpoint_states.get(session_id, "unknown")
            timeout = self.endpoint_timeouts.get(session_id)

            return {
                'session_id': endpoint.session_id,
                'tenant_id': endpoint.tenant_id,
                'local_port': endpoint.local_port,
                'remote_host': endpoint.remote_host,
                'remote_port': endpoint.remote_port,
                'codec': endpoint.codec,
                'state': state,
                'active': endpoint.active,
                'created_at': endpoint.created_at.isoformat(),
                'last_packet_time': endpoint.last_packet_time.isoformat() if endpoint.last_packet_time else None,
                'packet_count': endpoint.packet_count,
                'byte_count': endpoint.byte_count,
                'timeout_at': timeout.isoformat() if timeout else None
            }

        except Exception as e:
            logger.error(f"Error getting endpoint info for session {session_id}: {e}")
            return None

    async def list_active_endpoints(self) -> List[Dict[str, Any]]:
        """List all active endpoints with their information"""
        try:
            active_endpoints = []
            for session_id in self.rtp_server.session_endpoints.keys():
                endpoint_info = await self.get_endpoint_info(session_id)
                if endpoint_info:
                    active_endpoints.append(endpoint_info)

            return active_endpoints

        except Exception as e:
            logger.error(f"Error listing active endpoints: {e}")
            return []

    async def _cleanup_loop(self):
        """Periodic cleanup of expired endpoints"""
        try:
            while True:
                await asyncio.sleep(self.cleanup_interval)

                current_time = timezone.now()
                expired_sessions = []

                # Find expired sessions
                for session_id, timeout in self.endpoint_timeouts.items():
                    if current_time > timeout:
                        expired_sessions.append(session_id)

                # Clean up expired sessions
                for session_id in expired_sessions:
                    logger.info(f"Cleaning up expired endpoint for session {session_id}")
                    await self.destroy_session_endpoint(session_id)

                if expired_sessions:
                    logger.info(f"Cleaned up {len(expired_sessions)} expired endpoints")

        except asyncio.CancelledError:
            logger.info("RTP endpoint cleanup loop cancelled")
        except Exception as e:
            logger.error(f"Error in RTP endpoint cleanup loop: {e}")

    async def _health_check_loop(self):
        """Periodic health check of endpoints"""
        try:
            while True:
                await asyncio.sleep(self.health_check_interval)

                current_time = timezone.now()

                # Check endpoint health
                for session_id, endpoint in self.rtp_server.session_endpoints.items():
                    # Check if endpoint is receiving traffic
                    if endpoint.last_packet_time:
                        idle_time = (current_time - endpoint.last_packet_time).total_seconds()

                        # Automatically activate/deactivate based on traffic
                        if idle_time < 30 and self.endpoint_states.get(session_id) != "active":
                            await self.activate_endpoint(session_id)
                        elif idle_time > 120 and self.endpoint_states.get(session_id) == "active":
                            await self.deactivate_endpoint(session_id)

        except asyncio.CancelledError:
            logger.info("RTP endpoint health check loop cancelled")
        except Exception as e:
            logger.error(f"Error in RTP endpoint health check loop: {e}")

    async def _update_session_endpoint_info(self, session_id: str, endpoint: RTPEndpoint):
        """Update session with endpoint information"""
        try:
            session = await self.session_manager.get_session(session_id)
            if session:
                session.rtp_endpoint_port = endpoint.local_port
                session.metadata.update({
                    'rtp_endpoint': {
                        'local_port': endpoint.local_port,
                        'remote_host': endpoint.remote_host,
                        'remote_port': endpoint.remote_port,
                        'codec': endpoint.codec
                    }
                })
                await self.session_manager.update_session(session_id, session.to_dict())

        except Exception as e:
            logger.error(f"Error updating session endpoint info: {e}")

    async def _clear_session_endpoint_info(self, session_id: str):
        """Clear endpoint information from session"""
        try:
            session = await self.session_manager.get_session(session_id)
            if session:
                # session.rtp_endpoint_host = None
                # session.rtp_endpoint_port = None
                # session.metadata.pop('rtp_endpoint', None)
                await self.session_manager.update_session(session_id, session.to_dict())

        except Exception as e:
            logger.error(f"Error clearing session endpoint info: {e}")


async def cleanup_rtp_server():
    """Cleanup RTP server resources"""
    global _rtp_server
    if _rtp_server:
        await _rtp_server.stop()
        _rtp_server = None
        logger.info("RTP server cleaned up")
