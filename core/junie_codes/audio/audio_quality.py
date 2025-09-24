"""
Audio quality analysis and metrics for the AI Media Gateway system.

This module provides comprehensive audio quality assessment capabilities
including signal analysis, quality metrics calculation, and monitoring.

Features:
- Real-time audio quality analysis
- SNR (Signal-to-Noise Ratio) calculation
- Jitter and packet loss metrics
- MOS (Mean Opinion Score) estimation
- THD (Total Harmonic Distortion) analysis
- Quality trend monitoring
- Alert generation for quality issues
"""

import asyncio
import logging
import math
import struct
import numpy as np
import soundfile as sf
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import deque

from django.utils import timezone
from core.junie_codes.rtp_server import AudioFrame

logger = logging.getLogger(__name__)


# Audio codec conversion functions using numpy
def ulaw_to_linear(ulaw_data: bytes, sample_width: int = 2) -> bytes:
    """Convert μ-law encoded audio to linear PCM using numpy"""
    try:
        # Convert bytes to numpy array
        ulaw_samples = np.frombuffer(ulaw_data, dtype=np.uint8)
        
        # μ-law decompression algorithm
        ulaw_samples = ulaw_samples.astype(np.int16)
        sign = (ulaw_samples & 0x80) >> 7
        magnitude = ulaw_samples & 0x7F
        
        # Invert sign bit for processing
        magnitude = magnitude ^ 0x55
        
        # Extract exponent and mantissa
        exponent = (magnitude >> 4) & 0x07
        mantissa = magnitude & 0x0F
        
        # Calculate linear value
        linear = ((mantissa << 3) + 132) << exponent
        linear = np.where(sign == 0, linear, -linear)
        
        # Convert to 16-bit signed integers
        linear = np.clip(linear, -32768, 32767).astype(np.int16)
        
        return linear.tobytes()
    except Exception as e:
        logger.error(f"Error converting μ-law to linear: {e}")
        return ulaw_data


def alaw_to_linear(alaw_data: bytes, sample_width: int = 2) -> bytes:
    """Convert A-law encoded audio to linear PCM using numpy"""
    try:
        # Convert bytes to numpy array
        alaw_samples = np.frombuffer(alaw_data, dtype=np.uint8)
        
        # A-law decompression algorithm
        alaw_samples = alaw_samples.astype(np.int16)
        sign = (alaw_samples & 0x80) >> 7
        magnitude = alaw_samples & 0x7F
        
        # Invert even bits
        magnitude = magnitude ^ 0x55
        
        # Extract exponent and mantissa
        exponent = (magnitude >> 4) & 0x07
        mantissa = magnitude & 0x0F
        
        # Calculate linear value
        if exponent == 0:
            linear = (mantissa << 4) + 8
        else:
            linear = ((mantissa + 16) << (exponent + 3))
        
        linear = np.where(sign == 0, linear, -linear)
        
        # Convert to 16-bit signed integers
        linear = np.clip(linear, -32768, 32767).astype(np.int16)
        
        return linear.tobytes()
    except Exception as e:
        logger.error(f"Error converting A-law to linear: {e}")
        return alaw_data


def calculate_rms(audio_data: bytes, sample_width: int = 2) -> float:
    """Calculate RMS (Root Mean Square) of audio data using numpy"""
    try:
        if len(audio_data) == 0:
            return 0.0
        
        # Convert bytes to numpy array based on sample width
        if sample_width == 1:
            samples = np.frombuffer(audio_data, dtype=np.uint8).astype(np.float32) - 128
        elif sample_width == 2:
            samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
        else:
            # Default to 16-bit
            samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
        
        # Calculate RMS
        rms = np.sqrt(np.mean(samples ** 2))
        return float(rms)
    except Exception as e:
        logger.error(f"Error calculating RMS: {e}")
        return 0.0


class QualityLevel(Enum):
    """Audio quality levels"""
    EXCELLENT = "excellent"  # MOS 4.0-5.0
    GOOD = "good"           # MOS 3.0-4.0
    FAIR = "fair"           # MOS 2.0-3.0
    POOR = "poor"           # MOS 1.0-2.0
    BAD = "bad"             # MOS 0.0-1.0


@dataclass
class QualityMetrics:
    """Audio quality metrics data structure"""
    timestamp: datetime
    session_id: str
    snr_db: float = 0.0
    mos_score: float = 0.0
    jitter_ms: float = 0.0
    packet_loss_rate: float = 0.0
    latency_ms: float = 0.0
    bandwidth_kbps: float = 0.0
    distortion_thd: float = 0.0
    quality_level: QualityLevel = QualityLevel.FAIR
    metadata: Dict[str, Any] = field(default_factory=dict)


class AudioQualityAnalyzer:
    """Real-time audio quality analyzer"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.audio_buffer = deque(maxlen=1000)  # Keep last 1000 frames
        self.metrics_history = deque(maxlen=100)  # Keep last 100 measurements
        
        # Analysis parameters
        self.sample_rate = 8000  # Default telephony rate
        self.frame_duration_ms = 20  # 20ms frames
        self.analysis_window_size = 50  # Analyze every 50 frames
        
        # Statistics tracking
        self.stats = {
            'frames_analyzed': 0,
            'quality_alerts': 0,
            'average_mos': 0.0,
            'min_mos': 5.0,
            'max_mos': 0.0
        }
        
        # Current metrics
        self.current_metrics = QualityMetrics(
            timestamp=timezone.now(),
            session_id=session_id
        )
    
    def add_audio_frame(self, frame: AudioFrame) -> bool:
        """Add audio frame for quality analysis"""
        try:
            self.audio_buffer.append(frame)
            
            # Trigger analysis if buffer is full enough
            if len(self.audio_buffer) >= self.analysis_window_size:
                asyncio.create_task(self._analyze_quality())
                
            return True
            
        except Exception as e:
            logger.error(f"Error adding audio frame for quality analysis: {e}")
            return False
    
    async def _analyze_quality(self) -> QualityMetrics:
        """Perform comprehensive quality analysis"""
        try:
            if not self.audio_buffer:
                return self.current_metrics
            
            # Get recent frames for analysis
            frames = list(self.audio_buffer)[-self.analysis_window_size:]
            
            # Calculate individual metrics
            snr_db = self._calculate_snr(frames)
            jitter_ms = self._calculate_jitter(frames)
            packet_loss = self._calculate_packet_loss(frames)
            latency_ms = self._calculate_latency(frames)
            thd_percent = self._calculate_thd(frames)
            bandwidth = self._calculate_bandwidth(frames)
            
            # Calculate MOS score
            mos_score = self._calculate_mos(snr_db, jitter_ms, packet_loss)
            quality_level = self._determine_quality_level(mos_score)
            
            # Create metrics object
            metrics = QualityMetrics(
                timestamp=timezone.now(),
                session_id=self.session_id,
                snr_db=snr_db,
                mos_score=mos_score,
                jitter_ms=jitter_ms,
                packet_loss_rate=packet_loss,
                latency_ms=latency_ms,
                bandwidth_kbps=bandwidth,
                distortion_thd=thd_percent,
                quality_level=quality_level,
                metadata={
                    'frames_analyzed': len(frames),
                    'analysis_timestamp': timezone.now().isoformat()
                }
            )
            
            # Update current metrics and history
            self.current_metrics = metrics
            self.metrics_history.append(metrics)
            
            # Update statistics
            self._update_statistics(metrics)
            
            # Check for quality alerts
            await self._check_quality_alerts(metrics)
            
            return metrics
            
        except Exception as e:
            logger.error(f"Error analyzing audio quality: {e}")
            return self.current_metrics
    
    def _calculate_snr(self, frames: List[AudioFrame]) -> float:
        """Calculate Signal-to-Noise Ratio"""
        try:
            if not frames:
                return 0.0
            
            # Combine all audio data
            audio_data = b''.join([frame.payload for frame in frames])
            
            # Convert to linear PCM if needed
            if frames[0].codec == 'ulaw':
                audio_data = ulaw_to_linear(audio_data, 2)
            elif frames[0].codec == 'alaw':
                audio_data = alaw_to_linear(audio_data, 2)
            
            # Calculate RMS of entire signal
            signal_rms = calculate_rms(audio_data, 2)
            
            if signal_rms == 0:
                return 0.0
            
            # Estimate noise floor (use quietest 10% of samples)
            samples = struct.unpack(f'{len(audio_data)//2}h', audio_data)
            abs_samples = [abs(s) for s in samples]
            sorted_abs_samples = sorted(abs_samples)
            noise_samples = sorted_abs_samples[:len(sorted_abs_samples)//10]
            noise_rms = math.sqrt(sum(s**2 for s in noise_samples) / len(noise_samples)) if len(noise_samples) > 0 else 1
            
            # Calculate SNR in dB
            snr_linear = signal_rms / max(noise_rms, 1)
            snr_db = 20 * math.log10(max(snr_linear, 1e-10))
            
            return min(max(snr_db, 0), 60)  # Clamp between 0 and 60 dB
            
        except Exception as e:
            logger.error(f"Error calculating SNR: {e}")
            return 0.0
    
    def _calculate_jitter(self, frames: List[AudioFrame]) -> float:
        """Calculate packet jitter in milliseconds"""
        try:
            if len(frames) < 2:
                return 0.0
            
            # Calculate inter-arrival time variations
            arrival_times = []
            for i, frame in enumerate(frames):
                if hasattr(frame, 'processed_time') and frame.processed_time:
                    arrival_times.append(frame.processed_time.timestamp())
            
            if len(arrival_times) < 2:
                return 0.0
            
            # Calculate inter-packet intervals
            intervals = []
            for i in range(1, len(arrival_times)):
                interval = arrival_times[i] - arrival_times[i-1]
                intervals.append(interval * 1000)  # Convert to milliseconds
            
            if not intervals:
                return 0.0
            
            # Calculate jitter as standard deviation of intervals
            mean_interval = sum(intervals) / len(intervals)
            variance = sum((x - mean_interval)**2 for x in intervals) / len(intervals)
            jitter_ms = math.sqrt(variance)
            
            return min(jitter_ms, 1000)  # Cap at 1 second
            
        except Exception as e:
            logger.error(f"Error calculating jitter: {e}")
            return 0.0
    
    def _calculate_packet_loss(self, frames: List[AudioFrame]) -> float:
        """Calculate packet loss rate"""
        try:
            if len(frames) < 2:
                return 0.0
            
            # Check sequence numbers for gaps
            sequence_numbers = [frame.sequence_number for frame in frames if hasattr(frame, 'sequence_number')]
            
            if len(sequence_numbers) < 2:
                return 0.0
            
            # Sort sequence numbers
            sequence_numbers.sort()
            
            # Calculate expected vs received packets
            min_seq = sequence_numbers[0]
            max_seq = sequence_numbers[-1]
            expected_packets = max_seq - min_seq + 1
            received_packets = len(sequence_numbers)
            
            if expected_packets <= 0:
                return 0.0
            
            # Calculate loss rate
            lost_packets = expected_packets - received_packets
            loss_rate = max(0.0, lost_packets / expected_packets)
            
            return min(loss_rate, 1.0)  # Cap at 100%
            
        except Exception as e:
            logger.error(f"Error calculating packet loss: {e}")
            return 0.0
    
    def _calculate_latency(self, frames: List[AudioFrame]) -> float:
        """Calculate estimated latency"""
        try:
            # This is a simplified latency estimation
            # In practice, you would need timestamps from both ends
            
            if not frames:
                return 0.0
            
            # Use jitter as a proxy for latency variation
            jitter = self._calculate_jitter(frames)
            
            # Estimate base latency (typical for VoIP)
            base_latency = 50.0  # 50ms base latency
            estimated_latency = base_latency + (jitter * 2)
            
            return min(estimated_latency, 1000)  # Cap at 1 second
            
        except Exception as e:
            logger.error(f"Error calculating latency: {e}")
            return 0.0
    
    def _calculate_thd(self, frames: List[AudioFrame]) -> float:
        """Calculate Total Harmonic Distortion (simplified)"""
        try:
            if not frames:
                return 0.0
            
            # This is a simplified THD calculation
            # A full implementation would require FFT analysis
            
            # Combine audio data
            audio_data = b''.join([frame.payload for frame in frames])
            
            # Convert to linear PCM if needed
            if frames[0].codec == 'ulaw':
                audio_data = ulaw_to_linear(audio_data, 2)
            
            # Calculate simple distortion metric based on clipping
            samples = struct.unpack(f'{len(audio_data)//2}h', audio_data)
            abs_samples = [abs(s) for s in samples]
            max_value = max(abs_samples) if abs_samples else 0
            
            if max_value == 0:
                return 0.0
            
            # Count clipped samples (crude distortion measure)
            clipped = sum(1 for s in abs_samples if s >= 32000)  # Near max for 16-bit
            total = len(samples)
            
            thd_estimate = (clipped / total) * 100 if total > 0 else 0.0
            
            return min(thd_estimate, 50.0)  # Cap at 50%
            
        except Exception as e:
            logger.error(f"Error calculating THD: {e}")
            return 0.0
    
    def _calculate_bandwidth(self, frames: List[AudioFrame]) -> float:
        """Calculate bandwidth utilization"""
        try:
            if not frames or len(frames) < 2:
                return 0.0
            
            # Calculate total bytes
            total_bytes = sum(len(frame.payload) for frame in frames)
            
            # Calculate time span
            if hasattr(frames[0], 'processed_time') and hasattr(frames[-1], 'processed_time'):
                if frames[0].processed_time and frames[-1].processed_time:
                    time_span = (frames[-1].processed_time - frames[0].processed_time).total_seconds()
                    if time_span > 0:
                        bandwidth_bps = (total_bytes * 8) / time_span
                        return bandwidth_bps / 1000  # Convert to kbps
            
            # Fallback calculation
            bytes_per_frame = total_bytes / len(frames)
            frames_per_second = 1000 / self.frame_duration_ms  # Assuming 20ms frames
            bandwidth_bps = bytes_per_frame * frames_per_second * 8
            
            return bandwidth_bps / 1000  # Convert to kbps
            
        except Exception as e:
            logger.error(f"Error calculating bandwidth: {e}")
            return 0.0
    
    def _calculate_mos(self, snr_db: float, jitter_ms: float, packet_loss: float) -> float:
        """Calculate Mean Opinion Score (MOS) based on metrics"""
        try:
            # ITU-T G.107 E-model inspired MOS calculation
            
            # Start with perfect score
            r_factor = 93.2
            
            # SNR impact
            if snr_db < 10:
                r_factor -= (10 - snr_db) * 2
            elif snr_db > 40:
                r_factor += min(snr_db - 40, 10) * 0.5
            
            # Jitter impact
            if jitter_ms > 20:
                r_factor -= min(jitter_ms - 20, 100) * 0.2
            
            # Packet loss impact
            if packet_loss > 0:
                r_factor -= packet_loss * 100 * 2.5
            
            # Convert R-factor to MOS
            if r_factor < 0:
                mos = 1.0
            elif r_factor > 100:
                mos = 4.5
            else:
                mos = 1.0 + 0.035 * r_factor + r_factor * (r_factor - 60) * (100 - r_factor) * 7e-6
            
            return max(1.0, min(5.0, mos))
            
        except Exception as e:
            logger.error(f"Error calculating MOS: {e}")
            return 2.5  # Default fair quality
    
    def _determine_quality_level(self, mos_score: float) -> QualityLevel:
        """Determine quality level from MOS score"""
        if mos_score >= 4.0:
            return QualityLevel.EXCELLENT
        elif mos_score >= 3.0:
            return QualityLevel.GOOD
        elif mos_score >= 2.0:
            return QualityLevel.FAIR
        elif mos_score >= 1.0:
            return QualityLevel.POOR
        else:
            return QualityLevel.BAD
    
    def _update_statistics(self, metrics: QualityMetrics):
        """Update running statistics"""
        self.stats['frames_analyzed'] += self.analysis_window_size
        
        # Update MOS statistics
        if self.stats['average_mos'] == 0.0:
            self.stats['average_mos'] = metrics.mos_score
        else:
            alpha = 0.1  # Exponential moving average
            self.stats['average_mos'] = alpha * metrics.mos_score + (1 - alpha) * self.stats['average_mos']
        
        self.stats['min_mos'] = min(self.stats['min_mos'], metrics.mos_score)
        self.stats['max_mos'] = max(self.stats['max_mos'], metrics.mos_score)
    
    async def _check_quality_alerts(self, metrics: QualityMetrics):
        """Check for quality issues and generate alerts"""
        try:
            alerts = []
            
            # Check MOS threshold
            if metrics.mos_score < 2.0:
                alerts.append(f"Poor MOS score: {metrics.mos_score:.2f}")
            
            # Check packet loss
            if metrics.packet_loss_rate > 0.05:  # 5%
                alerts.append(f"High packet loss: {metrics.packet_loss_rate*100:.1f}%")
            
            # Check jitter
            if metrics.jitter_ms > 50:
                alerts.append(f"High jitter: {metrics.jitter_ms:.1f}ms")
            
            # Check SNR
            if metrics.snr_db < 15:
                alerts.append(f"Low SNR: {metrics.snr_db:.1f}dB")
            
            if alerts:
                self.stats['quality_alerts'] += len(alerts)
                logger.warning(f"Quality alerts for session {self.session_id}: {', '.join(alerts)}")
            
        except Exception as e:
            logger.error(f"Error checking quality alerts: {e}")
    
    def get_current_metrics(self) -> QualityMetrics:
        """Get current quality metrics"""
        return self.current_metrics
    
    def get_metrics_history(self) -> List[QualityMetrics]:
        """Get historical quality metrics"""
        return list(self.metrics_history)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get quality analysis statistics"""
        return {
            **self.stats,
            'session_id': self.session_id,
            'buffer_size': len(self.audio_buffer),
            'history_size': len(self.metrics_history),
            'current_quality_level': self.current_metrics.quality_level.value
        }


class AudioQualityManager:
    """Manager for all audio quality analysis operations"""
    
    def __init__(self):
        self.analyzers: Dict[str, AudioQualityAnalyzer] = {}
        self.global_stats = {
            'total_sessions': 0,
            'active_sessions': 0,
            'average_system_mos': 0.0,
            'total_quality_alerts': 0
        }
    
    def create_analyzer(self, session_id: str) -> AudioQualityAnalyzer:
        """Create quality analyzer for session"""
        try:
            if session_id in self.analyzers:
                return self.analyzers[session_id]
            
            analyzer = AudioQualityAnalyzer(session_id)
            self.analyzers[session_id] = analyzer
            
            self.global_stats['total_sessions'] += 1
            self.global_stats['active_sessions'] += 1
            
            logger.info(f"Quality analyzer created for session: {session_id}")
            return analyzer
            
        except Exception as e:
            logger.error(f"Error creating quality analyzer: {e}")
            return None
    
    def remove_analyzer(self, session_id: str) -> bool:
        """Remove quality analyzer for session"""
        try:
            if session_id in self.analyzers:
                del self.analyzers[session_id]
                self.global_stats['active_sessions'] = max(0, self.global_stats['active_sessions'] - 1)
                logger.info(f"Quality analyzer removed for session: {session_id}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error removing quality analyzer: {e}")
            return False
    
    def add_audio_frame(self, session_id: str, frame: AudioFrame) -> bool:
        """Add audio frame to quality analysis"""
        try:
            analyzer = self.analyzers.get(session_id)
            if analyzer:
                return analyzer.add_audio_frame(frame)
            
            return False
            
        except Exception as e:
            logger.error(f"Error adding frame to quality analysis: {e}")
            return False
    
    def get_session_metrics(self, session_id: str) -> Optional[QualityMetrics]:
        """Get current quality metrics for session"""
        analyzer = self.analyzers.get(session_id)
        if analyzer:
            return analyzer.get_current_metrics()
        return None
    
    def get_system_quality_report(self) -> Dict[str, Any]:
        """Get comprehensive system quality report"""
        try:
            session_reports = {}
            total_mos = 0.0
            session_count = 0
            total_alerts = 0
            
            for session_id, analyzer in self.analyzers.items():
                stats = analyzer.get_statistics()
                metrics = analyzer.get_current_metrics()
                
                session_reports[session_id] = {
                    'current_metrics': {
                        'mos_score': metrics.mos_score,
                        'snr_db': metrics.snr_db,
                        'jitter_ms': metrics.jitter_ms,
                        'packet_loss_rate': metrics.packet_loss_rate,
                        'quality_level': metrics.quality_level.value
                    },
                    'statistics': stats
                }
                
                total_mos += metrics.mos_score
                session_count += 1
                total_alerts += stats.get('quality_alerts', 0)
            
            # Update global statistics
            if session_count > 0:
                self.global_stats['average_system_mos'] = total_mos / session_count
            
            self.global_stats['total_quality_alerts'] = total_alerts
            
            return {
                'global_stats': self.global_stats,
                'session_reports': session_reports,
                'timestamp': timezone.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error generating quality report: {e}")
            return {'error': str(e)}


# Global quality manager
_global_quality_manager = None

def get_quality_manager() -> AudioQualityManager:
    """Get global audio quality manager"""
    global _global_quality_manager
    if _global_quality_manager is None:
        _global_quality_manager = AudioQualityManager()
    return _global_quality_manager
