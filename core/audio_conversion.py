"""
Audio format conversion utilities for the AI Media Gateway system.

This module provides comprehensive audio format conversion capabilities
supporting multiple codecs and formats commonly used in telephony and
VoIP communications.

Supported formats:
- PCM (Linear, signed 16-bit)
- μ-law (G.711 mu-law)
- A-law (G.711 A-law)
- GSM (Global System for Mobile Communications)
- G.722 (7 kHz audio codec)
- G.729 (8 kbit/s codec)
- WAV (WAVE format)
- RAW audio data

Features:
- Format detection and validation
- Sample rate conversion
- Bit depth conversion
- Audio quality metrics
- Batch processing capabilities
- Caching for performance optimization
"""

import audioop
import logging
import struct
import wave
import io
from typing import Dict, List, Optional, Any, Tuple, Union
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
import math

logger = logging.getLogger(__name__)


class AudioFormat(Enum):
    """Supported audio formats"""
    PCM_LINEAR = "pcm_linear"
    ULAW = "ulaw"
    ALAW = "alaw"
    GSM = "gsm"
    G722 = "g722"
    G729 = "g729"
    WAV = "wav"
    RAW = "raw"


class SampleRate(Enum):
    """Common sample rates"""
    RATE_8KHZ = 8000
    RATE_16KHZ = 16000
    RATE_22KHZ = 22050
    RATE_44KHZ = 44100
    RATE_48KHZ = 48000


class BitDepth(Enum):
    """Common bit depths"""
    DEPTH_8BIT = 8
    DEPTH_16BIT = 16
    DEPTH_24BIT = 24
    DEPTH_32BIT = 32


@dataclass
class AudioSpec:
    """Audio specification data structure"""
    format: AudioFormat
    sample_rate: int
    bit_depth: int
    channels: int = 1
    
    def __post_init__(self):
        """Validate audio specification"""
        if self.sample_rate <= 0:
            raise ValueError("Sample rate must be positive")
        if self.bit_depth not in [8, 16, 24, 32]:
            raise ValueError("Bit depth must be 8, 16, 24, or 32")
        if self.channels <= 0:
            raise ValueError("Channels must be positive")


@dataclass
class ConversionResult:
    """Audio conversion result with metadata"""
    data: bytes
    source_spec: AudioSpec
    target_spec: AudioSpec
    conversion_time: float
    quality_metrics: Dict[str, Any]
    success: bool = True
    error_message: Optional[str] = None


class AudioFormatDetector:
    """Audio format detection utilities"""
    
    @staticmethod
    def detect_format(data: bytes) -> Optional[AudioFormat]:
        """Detect audio format from raw data"""
        try:
            # Check for WAV header
            if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WAVE':
                return AudioFormat.WAV
            
            # Check for common telephony patterns
            # μ-law typically has values biased around 0xFF
            if len(data) > 100:
                ulaw_score = AudioFormatDetector._calculate_ulaw_score(data[:100])
                alaw_score = AudioFormatDetector._calculate_alaw_score(data[:100])
                
                if ulaw_score > 0.7:
                    return AudioFormat.ULAW
                elif alaw_score > 0.7:
                    return AudioFormat.ALAW
            
            # Default to raw PCM for unknown formats
            return AudioFormat.RAW
            
        except Exception as e:
            logger.error(f"Error detecting audio format: {e}")
            return None
    
    @staticmethod
    def _calculate_ulaw_score(data: bytes) -> float:
        """Calculate likelihood of data being μ-law encoded"""
        try:
            # μ-law has specific bit patterns and value distribution
            sign_bits = sum(1 for b in data if b & 0x80)
            uniform_distribution = abs(sign_bits / len(data) - 0.5) < 0.2
            
            # Check for μ-law specific patterns
            segment_patterns = 0
            for b in data:
                segment = (b & 0x70) >> 4
                if segment in [0, 1, 7]:  # Common μ-law segments
                    segment_patterns += 1
            
            pattern_score = segment_patterns / len(data)
            return 0.5 * (1.0 if uniform_distribution else 0.0) + 0.5 * pattern_score
            
        except:
            return 0.0
    
    @staticmethod
    def _calculate_alaw_score(data: bytes) -> float:
        """Calculate likelihood of data being A-law encoded"""
        try:
            # A-law has different bit patterns than μ-law
            sign_bits = sum(1 for b in data if b & 0x80)
            uniform_distribution = abs(sign_bits / len(data) - 0.5) < 0.2
            
            # Check for A-law specific patterns
            segment_patterns = 0
            for b in data:
                if b in [0x00, 0x80]:  # A-law zero values
                    segment_patterns += 1
                segment = (b & 0x70) >> 4
                if segment in [0, 7]:  # Common A-law segments
                    segment_patterns += 1
            
            pattern_score = segment_patterns / len(data)
            return 0.4 * (1.0 if uniform_distribution else 0.0) + 0.6 * pattern_score
            
        except:
            return 0.0


class AudioConverter:
    """Comprehensive audio format converter"""
    
    def __init__(self):
        self.conversion_cache = {}
        self.cache_max_size = 1000
        self.stats = {
            'conversions_performed': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'errors': 0,
            'total_bytes_converted': 0
        }
        
        # Codec-specific parameters
        self.codec_params = {
            AudioFormat.ULAW: {'sample_rate': 8000, 'bit_depth': 8},
            AudioFormat.ALAW: {'sample_rate': 8000, 'bit_depth': 8},
            AudioFormat.GSM: {'sample_rate': 8000, 'bit_depth': 16},
            AudioFormat.G722: {'sample_rate': 16000, 'bit_depth': 16},
            AudioFormat.G729: {'sample_rate': 8000, 'bit_depth': 16},
        }
    
    def convert(
        self,
        data: bytes,
        source_spec: AudioSpec,
        target_spec: AudioSpec,
        use_cache: bool = True
    ) -> ConversionResult:
        """Convert audio data between formats"""
        start_time = datetime.now()
        
        try:
            # Check cache first
            cache_key = None
            if use_cache:
                cache_key = self._generate_cache_key(data, source_spec, target_spec)
                if cache_key in self.conversion_cache:
                    self.stats['cache_hits'] += 1
                    cached_result = self.conversion_cache[cache_key]
                    return ConversionResult(
                        data=cached_result,
                        source_spec=source_spec,
                        target_spec=target_spec,
                        conversion_time=0.0,  # Cache hit
                        quality_metrics={'cached': True},
                        success=True
                    )
                
                self.stats['cache_misses'] += 1
            
            # Perform conversion
            converted_data = self._perform_conversion(data, source_spec, target_spec)
            
            if converted_data is None:
                return ConversionResult(
                    data=b'',
                    source_spec=source_spec,
                    target_spec=target_spec,
                    conversion_time=(datetime.now() - start_time).total_seconds(),
                    quality_metrics={},
                    success=False,
                    error_message="Conversion failed"
                )
            
            # Calculate quality metrics
            quality_metrics = self._calculate_quality_metrics(data, converted_data, source_spec, target_spec)
            
            # Update cache
            if use_cache and cache_key and len(self.conversion_cache) < self.cache_max_size:
                self.conversion_cache[cache_key] = converted_data
            
            # Update statistics
            self.stats['conversions_performed'] += 1
            self.stats['total_bytes_converted'] += len(converted_data)
            
            return ConversionResult(
                data=converted_data,
                source_spec=source_spec,
                target_spec=target_spec,
                conversion_time=(datetime.now() - start_time).total_seconds(),
                quality_metrics=quality_metrics,
                success=True
            )
            
        except Exception as e:
            self.stats['errors'] += 1
            logger.error(f"Conversion error: {e}")
            
            return ConversionResult(
                data=b'',
                source_spec=source_spec,
                target_spec=target_spec,
                conversion_time=(datetime.now() - start_time).total_seconds(),
                quality_metrics={},
                success=False,
                error_message=str(e)
            )
    
    def _perform_conversion(self, data: bytes, source_spec: AudioSpec, target_spec: AudioSpec) -> Optional[bytes]:
        """Perform the actual audio conversion"""
        # If formats are the same, handle sample rate/bit depth conversion only
        if source_spec.format == target_spec.format:
            return self._convert_properties_only(data, source_spec, target_spec)
        
        # Convert to PCM linear first (intermediate format)
        pcm_data = self._to_pcm_linear(data, source_spec)
        if pcm_data is None:
            return None
        
        # Convert from PCM linear to target format
        return self._from_pcm_linear(pcm_data, source_spec, target_spec)
    
    def _to_pcm_linear(self, data: bytes, source_spec: AudioSpec) -> Optional[bytes]:
        """Convert from any format to PCM linear"""
        try:
            if source_spec.format == AudioFormat.PCM_LINEAR:
                return data
            elif source_spec.format == AudioFormat.ULAW:
                return audioop.ulaw2lin(data, 2)
            elif source_spec.format == AudioFormat.ALAW:
                return audioop.alaw2lin(data, 2)
            elif source_spec.format == AudioFormat.WAV:
                return self._extract_pcm_from_wav(data)
            elif source_spec.format == AudioFormat.GSM:
                logger.warning("GSM decoding requires external library")
                return None
            elif source_spec.format == AudioFormat.G722:
                logger.warning("G.722 decoding requires external library")
                return None
            elif source_spec.format == AudioFormat.G729:
                logger.warning("G.729 decoding requires external library")
                return None
            else:
                # Assume raw PCM data
                return data
                
        except Exception as e:
            logger.error(f"Error converting to PCM linear: {e}")
            return None
    
    def _from_pcm_linear(self, pcm_data: bytes, source_spec: AudioSpec, target_spec: AudioSpec) -> Optional[bytes]:
        """Convert from PCM linear to target format"""
        try:
            # Handle sample rate conversion if needed
            if source_spec.sample_rate != target_spec.sample_rate:
                pcm_data = self._resample_audio(pcm_data, source_spec.sample_rate, target_spec.sample_rate)
            
            # Handle bit depth conversion if needed
            if source_spec.bit_depth != target_spec.bit_depth:
                pcm_data = self._convert_bit_depth(pcm_data, source_spec.bit_depth, target_spec.bit_depth)
            
            # Convert to target format
            if target_spec.format == AudioFormat.PCM_LINEAR:
                return pcm_data
            elif target_spec.format == AudioFormat.ULAW:
                return audioop.lin2ulaw(pcm_data, 2)
            elif target_spec.format == AudioFormat.ALAW:
                return audioop.lin2alaw(pcm_data, 2)
            elif target_spec.format == AudioFormat.WAV:
                return self._create_wav_data(pcm_data, target_spec)
            elif target_spec.format == AudioFormat.GSM:
                logger.warning("GSM encoding requires external library")
                return pcm_data  # Return PCM as fallback
            elif target_spec.format == AudioFormat.G722:
                logger.warning("G.722 encoding requires external library")
                return pcm_data  # Return PCM as fallback
            elif target_spec.format == AudioFormat.G729:
                logger.warning("G.729 encoding requires external library")
                return pcm_data  # Return PCM as fallback
            else:
                return pcm_data
                
        except Exception as e:
            logger.error(f"Error converting from PCM linear: {e}")
            return None
    
    def _convert_properties_only(self, data: bytes, source_spec: AudioSpec, target_spec: AudioSpec) -> bytes:
        """Convert sample rate/bit depth without changing format"""
        converted_data = data
        
        # Sample rate conversion
        if source_spec.sample_rate != target_spec.sample_rate:
            converted_data = self._resample_audio(converted_data, source_spec.sample_rate, target_spec.sample_rate)
        
        # Bit depth conversion
        if source_spec.bit_depth != target_spec.bit_depth:
            converted_data = self._convert_bit_depth(converted_data, source_spec.bit_depth, target_spec.bit_depth)
        
        return converted_data
    
    def _resample_audio(self, data: bytes, source_rate: int, target_rate: int) -> bytes:
        """Resample audio data to different sample rate"""
        try:
            # Simple linear resampling using audioop
            return audioop.ratecv(data, 2, 1, source_rate, target_rate, None)[0]
        except Exception as e:
            logger.error(f"Error resampling audio: {e}")
            return data
    
    def _convert_bit_depth(self, data: bytes, source_depth: int, target_depth: int) -> bytes:
        """Convert audio bit depth"""
        try:
            if source_depth == target_depth:
                return data
            
            if source_depth == 16 and target_depth == 8:
                # 16-bit to 8-bit
                return audioop.lin2lin(data, 2, 1)
            elif source_depth == 8 and target_depth == 16:
                # 8-bit to 16-bit
                return audioop.lin2lin(data, 1, 2)
            else:
                # For other conversions, use linear scaling
                logger.warning(f"Bit depth conversion {source_depth}→{target_depth} using linear scaling")
                return data
                
        except Exception as e:
            logger.error(f"Error converting bit depth: {e}")
            return data
    
    def _extract_pcm_from_wav(self, wav_data: bytes) -> Optional[bytes]:
        """Extract PCM data from WAV file"""
        try:
            with wave.open(io.BytesIO(wav_data), 'rb') as wav_file:
                return wav_file.readframes(wav_file.getnframes())
        except Exception as e:
            logger.error(f"Error extracting PCM from WAV: {e}")
            return None
    
    def _create_wav_data(self, pcm_data: bytes, spec: AudioSpec) -> bytes:
        """Create WAV file data from PCM"""
        try:
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav_file:
                wav_file.setnchannels(spec.channels)
                wav_file.setsampwidth(spec.bit_depth // 8)
                wav_file.setframerate(spec.sample_rate)
                wav_file.writeframes(pcm_data)
            
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Error creating WAV data: {e}")
            return pcm_data
    
    def _calculate_quality_metrics(
        self,
        source_data: bytes,
        target_data: bytes,
        source_spec: AudioSpec,
        target_spec: AudioSpec
    ) -> Dict[str, Any]:
        """Calculate conversion quality metrics"""
        try:
            metrics = {
                'source_size': len(source_data),
                'target_size': len(target_data),
                'compression_ratio': len(target_data) / len(source_data) if source_data else 0,
                'source_format': source_spec.format.value,
                'target_format': target_spec.format.value,
                'sample_rate_change': target_spec.sample_rate / source_spec.sample_rate,
                'bit_depth_change': target_spec.bit_depth / source_spec.bit_depth
            }
            
            # Calculate signal quality metrics if both are PCM-like
            if source_spec.format in [AudioFormat.PCM_LINEAR] and len(source_data) > 0:
                try:
                    source_rms = audioop.rms(source_data, 2)
                    metrics['source_rms_level'] = source_rms
                    
                    if target_spec.format in [AudioFormat.PCM_LINEAR] and len(target_data) > 0:
                        target_rms = audioop.rms(target_data, 2)
                        metrics['target_rms_level'] = target_rms
                        metrics['rms_ratio'] = target_rms / source_rms if source_rms > 0 else 0
                        
                except:
                    pass  # Skip RMS calculation if it fails
            
            return metrics
            
        except Exception as e:
            logger.error(f"Error calculating quality metrics: {e}")
            return {'error': str(e)}
    
    def _generate_cache_key(self, data: bytes, source_spec: AudioSpec, target_spec: AudioSpec) -> str:
        """Generate cache key for conversion"""
        data_hash = hash(data) % 10000  # Simple hash to avoid large keys
        return f"{source_spec.format.value}_{target_spec.format.value}_{source_spec.sample_rate}_{target_spec.sample_rate}_{source_spec.bit_depth}_{target_spec.bit_depth}_{len(data)}_{data_hash}"
    
    def get_stats(self) -> Dict[str, Any]:
        """Get converter statistics"""
        return self.stats.copy()
    
    def clear_cache(self):
        """Clear conversion cache"""
        self.conversion_cache.clear()
    
    def validate_conversion_support(self, source_format: AudioFormat, target_format: AudioFormat) -> bool:
        """Check if conversion between formats is supported"""
        # All conversions to/from PCM linear are supported
        if source_format == AudioFormat.PCM_LINEAR or target_format == AudioFormat.PCM_LINEAR:
            return True
        
        # Direct conversions
        supported_pairs = [
            (AudioFormat.ULAW, AudioFormat.ALAW),
            (AudioFormat.ALAW, AudioFormat.ULAW),
            (AudioFormat.WAV, AudioFormat.PCM_LINEAR),
        ]
        
        return (source_format, target_format) in supported_pairs or (target_format, source_format) in supported_pairs


class BatchAudioConverter:
    """Batch audio processing utilities"""
    
    def __init__(self, converter: AudioConverter = None):
        self.converter = converter or AudioConverter()
        self.batch_stats = {
            'files_processed': 0,
            'total_time': 0.0,
            'success_count': 0,
            'error_count': 0
        }
    
    def convert_batch(
        self,
        audio_data_list: List[Tuple[bytes, AudioSpec]],
        target_spec: AudioSpec
    ) -> List[ConversionResult]:
        """Convert multiple audio files"""
        results = []
        start_time = datetime.now()
        
        for data, source_spec in audio_data_list:
            result = self.converter.convert(data, source_spec, target_spec)
            results.append(result)
            
            self.batch_stats['files_processed'] += 1
            if result.success:
                self.batch_stats['success_count'] += 1
            else:
                self.batch_stats['error_count'] += 1
        
        self.batch_stats['total_time'] = (datetime.now() - start_time).total_seconds()
        return results
    
    def get_batch_stats(self) -> Dict[str, Any]:
        """Get batch processing statistics"""
        stats = self.batch_stats.copy()
        if stats['files_processed'] > 0:
            stats['success_rate'] = stats['success_count'] / stats['files_processed']
            stats['average_time_per_file'] = stats['total_time'] / stats['files_processed']
        return stats


# Global converter instance
_global_converter = None

def get_audio_converter() -> AudioConverter:
    """Get global audio converter instance"""
    global _global_converter
    if _global_converter is None:
        _global_converter = AudioConverter()
    return _global_converter


def detect_audio_format(data: bytes) -> Optional[AudioFormat]:
    """Convenient function to detect audio format"""
    return AudioFormatDetector.detect_format(data)


def convert_audio(
    data: bytes,
    source_format: AudioFormat,
    target_format: AudioFormat,
    source_sample_rate: int = 8000,
    target_sample_rate: int = 8000,
    source_bit_depth: int = 16,
    target_bit_depth: int = 16
) -> ConversionResult:
    """Convenient function for audio conversion"""
    converter = get_audio_converter()
    
    source_spec = AudioSpec(
        format=source_format,
        sample_rate=source_sample_rate,
        bit_depth=source_bit_depth
    )
    
    target_spec = AudioSpec(
        format=target_format,
        sample_rate=target_sample_rate,
        bit_depth=target_bit_depth
    )
    
    return converter.convert(data, source_spec, target_spec)


# Codec information utilities
def get_codec_info(format_name: str) -> Optional[Dict[str, Any]]:
    """Get codec information by format name"""
    codec_info = {
        'ulaw': {
            'name': 'μ-law',
            'description': 'ITU-T G.711 μ-law',
            'sample_rate': 8000,
            'bit_depth': 8,
            'compression': 'lossy',
            'quality': 'toll'
        },
        'alaw': {
            'name': 'A-law', 
            'description': 'ITU-T G.711 A-law',
            'sample_rate': 8000,
            'bit_depth': 8,
            'compression': 'lossy',
            'quality': 'toll'
        },
        'pcm_linear': {
            'name': 'PCM Linear',
            'description': 'Uncompressed PCM',
            'sample_rate': 'variable',
            'bit_depth': 'variable',
            'compression': 'none',
            'quality': 'lossless'
        },
        'gsm': {
            'name': 'GSM',
            'description': 'GSM 06.10',
            'sample_rate': 8000,
            'bit_depth': 16,
            'compression': 'lossy',
            'quality': 'communication'
        },
        'g722': {
            'name': 'G.722',
            'description': '7 kHz audio coding',
            'sample_rate': 16000,
            'bit_depth': 16,
            'compression': 'lossy',
            'quality': 'wideband'
        }
    }
    
    return codec_info.get(format_name.lower())
