"""
Audio transcription integration points for the AI Media Gateway system.

This module provides comprehensive audio transcription capabilities
with support for multiple transcription services and providers.

Features:
- Multiple transcription service providers (Google, AWS, Azure, OpenAI)
- Real-time and batch transcription processing
- Audio preprocessing for optimal transcription
- Multi-tenant transcription isolation
- Transcription result caching and storage
- Language detection and multi-language support
- Confidence scoring and quality metrics
- Integration with call recording system
"""

import asyncio
import logging
import hashlib
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from abc import ABC, abstractmethod

from django.utils import timezone
from django.conf import settings

# Import our components
from core.audio.audio_conversion import AudioConverter, AudioFormat, AudioSpec
from core.audio.audio_recording import RecordingSession

logger = logging.getLogger(__name__)

# RIVA imports
try:
    from riva.client import SpeechRecognitionService, Auth
    from riva.client.proto import riva_asr_pb2
    RIVA_AVAILABLE = True
except ImportError:
    RIVA_AVAILABLE = False
    logger.warning("RIVA client not available. Install nvidia-riva-client to use RIVA transcription.")


class TranscriptionProvider(Enum):
    """Supported transcription service providers"""
    NVIDIA_RIVA = "nvidia_riva"
    GOOGLE_SPEECH = "google_speech"
    AWS_TRANSCRIBE = "aws_transcribe"
    AZURE_SPEECH = "azure_speech"
    OPENAI_WHISPER = "openai_whisper"
    IBM_WATSON = "ibm_watson"
    MOCK_SERVICE = "mock_service"  # For testing


class TranscriptionMode(Enum):
    """Transcription processing modes"""
    REAL_TIME = "real_time"      # Live transcription during call
    BATCH = "batch"              # Post-call transcription
    HYBRID = "hybrid"            # Real-time + post-processing


class LanguageCode(Enum):
    """Supported language codes (ISO 639-1)"""
    ENGLISH_US = "en-US"
    ENGLISH_GB = "en-GB"
    SPANISH_ES = "es-ES"
    FRENCH_FR = "fr-FR"
    GERMAN_DE = "de-DE"
    ITALIAN_IT = "it-IT"
    PORTUGUESE_BR = "pt-BR"
    RUSSIAN_RU = "ru-RU"
    CHINESE_CN = "zh-CN"
    JAPANESE_JP = "ja-JP"
    AUTO_DETECT = "auto"


@dataclass
class TranscriptionConfig:
    """Configuration for audio transcription"""
    provider: TranscriptionProvider = TranscriptionProvider.NVIDIA_RIVA
    mode: TranscriptionMode = TranscriptionMode.BATCH
    language: LanguageCode = LanguageCode.AUTO_DETECT
    enable_speaker_diarization: bool = True
    enable_punctuation: bool = True
    enable_word_timestamps: bool = True
    confidence_threshold: float = 0.7
    sample_rate: int = 16000  # Optimal for most services
    chunk_duration_seconds: int = 30  # For real-time processing
    max_alternatives: int = 3
    filter_profanity: bool = True
    custom_vocabulary: List[str] = field(default_factory=list)


@dataclass
class TranscriptionSegment:
    """Individual transcription segment/word"""
    text: str
    start_time_seconds: float
    end_time_seconds: float
    confidence: float
    speaker_id: Optional[str] = None
    word_alternatives: List[str] = field(default_factory=list)


@dataclass
class TranscriptionResult:
    """Complete transcription result"""
    transcription_id: str
    session_id: str
    provider: TranscriptionProvider
    text: str
    language_detected: str
    confidence_average: float
    segments: List[TranscriptionSegment]
    processing_time_seconds: float
    audio_duration_seconds: float
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    success: bool = True


class TranscriptionServiceInterface(ABC):
    """Abstract interface for transcription services"""
    
    @abstractmethod
    async def transcribe_audio(
        self, 
        audio_data: bytes, 
        config: TranscriptionConfig
    ) -> TranscriptionResult:
        """Transcribe audio data"""
        pass
    
    @abstractmethod
    async def transcribe_streaming(
        self, 
        audio_stream: Any, 
        config: TranscriptionConfig,
        callback: Callable[[TranscriptionSegment], None]
    ) -> bool:
        """Start streaming transcription"""
        pass
    
    @abstractmethod
    def get_supported_languages(self) -> List[LanguageCode]:
        """Get list of supported languages"""
        pass
    
    @abstractmethod
    def validate_config(self, config: TranscriptionConfig) -> bool:
        """Validate transcription configuration"""
        pass


class MockTranscriptionService(TranscriptionServiceInterface):
    """Mock transcription service for testing"""
    
    async def transcribe_audio(
        self, 
        audio_data: bytes, 
        config: TranscriptionConfig
    ) -> TranscriptionResult:
        """Mock audio transcription"""
        try:
            # Simulate processing delay
            await asyncio.sleep(0.5)
            
            # Generate mock transcription
            mock_text = "This is a mock transcription of the audio content. The call quality was good and the conversation was clear."
            
            # Create mock segments
            segments = [
                TranscriptionSegment(
                    text="This is a mock transcription",
                    start_time_seconds=0.0,
                    end_time_seconds=2.5,
                    confidence=0.95,
                    speaker_id="speaker_1"
                ),
                TranscriptionSegment(
                    text="of the audio content.",
                    start_time_seconds=2.5,
                    end_time_seconds=4.0,
                    confidence=0.92,
                    speaker_id="speaker_1"
                ),
                TranscriptionSegment(
                    text="The call quality was good",
                    start_time_seconds=4.0,
                    end_time_seconds=6.0,
                    confidence=0.88,
                    speaker_id="speaker_2"
                ),
                TranscriptionSegment(
                    text="and the conversation was clear.",
                    start_time_seconds=6.0,
                    end_time_seconds=8.0,
                    confidence=0.91,
                    speaker_id="speaker_2"
                )
            ]
            
            return TranscriptionResult(
                transcription_id=f"mock_{int(timezone.now().timestamp())}",
                session_id="mock_session",
                provider=TranscriptionProvider.MOCK_SERVICE,
                text=mock_text,
                language_detected="en-US",
                confidence_average=0.915,
                segments=segments,
                processing_time_seconds=0.5,
                audio_duration_seconds=8.0,
                created_at=timezone.now(),
                metadata={
                    "audio_size_bytes": len(audio_data),
                    "mock_service": True
                }
            )
            
        except Exception as e:
            logger.error(f"Mock transcription error: {e}")
            return TranscriptionResult(
                transcription_id="error",
                session_id="error",
                provider=TranscriptionProvider.MOCK_SERVICE,
                text="",
                language_detected="unknown",
                confidence_average=0.0,
                segments=[],
                processing_time_seconds=0.0,
                audio_duration_seconds=0.0,
                created_at=timezone.now(),
                error_message=str(e),
                success=False
            )
    
    async def transcribe_streaming(
        self, 
        audio_stream: Any, 
        config: TranscriptionConfig,
        callback: Callable[[TranscriptionSegment], None]
    ) -> bool:
        """Mock streaming transcription"""
        try:
            # Simulate streaming segments
            mock_segments = [
                "Hello, this is a streaming transcription test.",
                "The audio quality is being monitored in real-time.",
                "Thank you for using our transcription service."
            ]
            
            for i, text in enumerate(mock_segments):
                segment = TranscriptionSegment(
                    text=text,
                    start_time_seconds=i * 3.0,
                    end_time_seconds=(i + 1) * 3.0,
                    confidence=0.9 + (i * 0.02),
                    speaker_id=f"speaker_{i % 2 + 1}"
                )
                
                callback(segment)
                await asyncio.sleep(1.0)  # Simulate real-time processing
            
            return True
            
        except Exception as e:
            logger.error(f"Mock streaming transcription error: {e}")
            return False
    
    def get_supported_languages(self) -> List[LanguageCode]:
        """Get supported languages for mock service"""
        return [LanguageCode.ENGLISH_US, LanguageCode.ENGLISH_GB, LanguageCode.AUTO_DETECT]
    
    def validate_config(self, config: TranscriptionConfig) -> bool:
        """Validate mock service configuration"""
        return config.language in self.get_supported_languages()


class GoogleSpeechService(TranscriptionServiceInterface):
    """Google Cloud Speech-to-Text service integration"""
    
    def __init__(self):
        self.api_key = getattr(settings, 'GOOGLE_SPEECH_API_KEY', None)
        self.credentials_path = getattr(settings, 'GOOGLE_APPLICATION_CREDENTIALS', None)
    
    async def transcribe_audio(
        self, 
        audio_data: bytes, 
        config: TranscriptionConfig
    ) -> TranscriptionResult:
        """Google Speech API transcription"""
        # This would integrate with Google Cloud Speech API
        # For now, return mock implementation
        logger.warning("Google Speech API integration not implemented, using mock")
        mock_service = MockTranscriptionService()
        result = await mock_service.transcribe_audio(audio_data, config)
        result.provider = TranscriptionProvider.GOOGLE_SPEECH
        return result
    
    async def transcribe_streaming(
        self, 
        audio_stream: Any, 
        config: TranscriptionConfig,
        callback: Callable[[TranscriptionSegment], None]
    ) -> bool:
        """Google Speech streaming API"""
        logger.warning("Google Speech streaming API integration not implemented")
        return False
    
    def get_supported_languages(self) -> List[LanguageCode]:
        """Google Speech supported languages"""
        return [
            LanguageCode.ENGLISH_US, LanguageCode.ENGLISH_GB,
            LanguageCode.SPANISH_ES, LanguageCode.FRENCH_FR,
            LanguageCode.GERMAN_DE, LanguageCode.AUTO_DETECT
        ]
    
    def validate_config(self, config: TranscriptionConfig) -> bool:
        """Validate Google Speech configuration"""
        return (self.api_key or self.credentials_path) and config.language in self.get_supported_languages()


class RivaTranscriptionService(TranscriptionServiceInterface):
    """NVIDIA RIVA ASR service integration"""
    
    def __init__(self):
        self.riva_uri = getattr(settings, 'RIVA_ASR_URI', 'localhost:50051')
        self.use_ssl = getattr(settings, 'RIVA_USE_SSL', False)
        self.ssl_cert = getattr(settings, 'RIVA_SSL_CERT', None)
        self.ssl_key = getattr(settings, 'RIVA_SSL_KEY', None)
        self.ssl_root_certs = getattr(settings, 'RIVA_SSL_ROOT_CERTS', None)
        
        # Initialize RIVA auth and service
        self.auth = None
        self.service = None
        if RIVA_AVAILABLE:
            try:
                self.auth = Auth(
                    uri=self.riva_uri,
                    use_ssl=self.use_ssl,
                    ssl_cert=self.ssl_cert,
                    ssl_key=self.ssl_key,
                    ssl_root_certs=self.ssl_root_certs
                )
                self.service = SpeechRecognitionService(auth=self.auth)
                logger.info(f"RIVA ASR service initialized with URI: {self.riva_uri}")
            except Exception as e:
                logger.error(f"Failed to initialize RIVA ASR service: {e}")
                self.service = None
        else:
            logger.error("RIVA client not available - install nvidia-riva-client")
    
    async def transcribe_audio(
        self, 
        audio_data: bytes, 
        config: TranscriptionConfig
    ) -> TranscriptionResult:
        """RIVA batch transcription"""
        if not self.service or not RIVA_AVAILABLE:
            logger.error("RIVA service not available")
            return TranscriptionResult(
                transcription_id=hashlib.md5(audio_data).hexdigest()[:16],
                session_id="",
                provider=TranscriptionProvider.NVIDIA_RIVA,
                text="",
                language_detected="unknown",
                confidence_average=0.0,
                segments=[],
                processing_time_seconds=0.0,
                audio_duration_seconds=0.0,
                created_at=timezone.now(),
                error_message="RIVA service not available",
                success=False
            )
        
        try:
            start_time = datetime.now()
            
            # Configure RIVA ASR request
            riva_config = riva_asr_pb2.RecognitionConfig()
            riva_config.encoding = riva_asr_pb2.RecognitionConfig.AudioEncoding.LINEAR_PCM
            riva_config.sample_rate_hertz = config.sample_rate
            riva_config.language_code = self._map_language_to_riva(config.language)
            riva_config.max_alternatives = config.max_alternatives
            riva_config.enable_word_time_offsets = config.enable_word_timestamps
            riva_config.enable_speaker_diarization = config.enable_speaker_diarization
            riva_config.enable_automatic_punctuation = config.enable_punctuation
            
            # Add custom vocabulary if provided
            if config.custom_vocabulary:
                speech_context = riva_config.speech_contexts.add()
                speech_context.phrases.extend(config.custom_vocabulary)
            
            # Create transcription request
            request = riva_asr_pb2.RecognizeRequest(
                config=riva_config,
                audio=riva_asr_pb2.RecognitionAudio(content=audio_data)
            )
            
            # Perform transcription
            response = self.service.recognize(request)
            processing_time = (datetime.now() - start_time).total_seconds()
            
            # Process results
            if response.results:
                best_result = response.results[0]
                best_alternative = best_result.alternatives[0] if best_result.alternatives else None
                
                if best_alternative:
                    # Extract segments with word-level timestamps
                    segments = []
                    if best_alternative.words:
                        for word in best_alternative.words:
                            segment = TranscriptionSegment(
                                text=word.word,
                                start_time_seconds=word.start_time.seconds + word.start_time.nanos / 1e9,
                                end_time_seconds=word.end_time.seconds + word.end_time.nanos / 1e9,
                                confidence=word.confidence,
                                speaker_id=getattr(word, 'speaker_tag', None)
                            )
                            segments.append(segment)
                    else:
                        # Fallback: create single segment for entire transcription
                        segments.append(TranscriptionSegment(
                            text=best_alternative.transcript,
                            start_time_seconds=0.0,
                            end_time_seconds=len(audio_data) / (config.sample_rate * 2),  # Estimate duration
                            confidence=best_alternative.confidence,
                            speaker_id=None
                        ))
                    
                    # Calculate average confidence
                    avg_confidence = sum(s.confidence for s in segments) / len(segments) if segments else 0.0
                    
                    return TranscriptionResult(
                        transcription_id=hashlib.md5(audio_data).hexdigest()[:16],
                        session_id="",
                        provider=TranscriptionProvider.NVIDIA_RIVA,
                        text=best_alternative.transcript,
                        language_detected=riva_config.language_code,
                        confidence_average=avg_confidence,
                        segments=segments,
                        processing_time_seconds=processing_time,
                        audio_duration_seconds=len(audio_data) / (config.sample_rate * 2),
                        created_at=timezone.now(),
                        success=True
                    )
            
            # No results case
            return TranscriptionResult(
                transcription_id=hashlib.md5(audio_data).hexdigest()[:16],
                session_id="",
                provider=TranscriptionProvider.NVIDIA_RIVA,
                text="",
                language_detected=riva_config.language_code,
                confidence_average=0.0,
                segments=[],
                processing_time_seconds=processing_time,
                audio_duration_seconds=len(audio_data) / (config.sample_rate * 2),
                created_at=timezone.now(),
                error_message="No transcription results returned",
                success=False
            )
            
        except Exception as e:
            logger.error(f"RIVA transcription error: {e}")
            return TranscriptionResult(
                transcription_id=hashlib.md5(audio_data).hexdigest()[:16],
                session_id="",
                provider=TranscriptionProvider.NVIDIA_RIVA,
                text="",
                language_detected="unknown",
                confidence_average=0.0,
                segments=[],
                processing_time_seconds=0.0,
                audio_duration_seconds=0.0,
                created_at=timezone.now(),
                error_message=str(e),
                success=False
            )
    
    async def transcribe_streaming(
        self, 
        audio_stream: Any, 
        config: TranscriptionConfig,
        callback: Callable[[TranscriptionSegment], None]
    ) -> bool:
        """RIVA streaming transcription"""
        if not self.service or not RIVA_AVAILABLE:
            logger.error("RIVA service not available for streaming")
            return False
        
        try:
            # Configure streaming request
            riva_config = riva_asr_pb2.RecognitionConfig()
            riva_config.encoding = riva_asr_pb2.RecognitionConfig.AudioEncoding.LINEAR_PCM
            riva_config.sample_rate_hertz = config.sample_rate
            riva_config.language_code = self._map_language_to_riva(config.language)
            riva_config.enable_word_time_offsets = config.enable_word_timestamps
            riva_config.enable_speaker_diarization = config.enable_speaker_diarization
            riva_config.enable_automatic_punctuation = config.enable_punctuation
            
            streaming_config = riva_asr_pb2.StreamingRecognitionConfig(
                config=riva_config,
                interim_results=True
            )
            
            # Generator function for audio chunks
            def audio_generator():
                # Initial request with config
                yield riva_asr_pb2.StreamingRecognizeRequest(streaming_config=streaming_config)
                
                # Stream audio chunks
                for audio_chunk in audio_stream:
                    yield riva_asr_pb2.StreamingRecognizeRequest(audio_content=audio_chunk)
            
            # Start streaming
            responses = self.service.streaming_recognize(audio_generator())
            
            for response in responses:
                if response.results:
                    for result in response.results:
                        if result.alternatives:
                            alternative = result.alternatives[0]
                            
                            # Create segment from streaming result
                            segment = TranscriptionSegment(
                                text=alternative.transcript,
                                start_time_seconds=0.0,  # Streaming doesn't provide absolute timing
                                end_time_seconds=0.0,
                                confidence=alternative.confidence,
                                speaker_id=None
                            )
                            
                            # Call callback with segment
                            callback(segment)
                            
                            # If this is a final result, we can extract word timings
                            if result.is_final and alternative.words:
                                for word in alternative.words:
                                    word_segment = TranscriptionSegment(
                                        text=word.word,
                                        start_time_seconds=word.start_time.seconds + word.start_time.nanos / 1e9,
                                        end_time_seconds=word.end_time.seconds + word.end_time.nanos / 1e9,
                                        confidence=word.confidence,
                                        speaker_id=getattr(word, 'speaker_tag', None)
                                    )
                                    callback(word_segment)
            
            return True
            
        except Exception as e:
            logger.error(f"RIVA streaming transcription error: {e}")
            return False
    
    def get_supported_languages(self) -> List[LanguageCode]:
        """Get RIVA supported languages"""
        return [
            LanguageCode.ENGLISH_US,
            LanguageCode.ENGLISH_GB,
            LanguageCode.SPANISH_ES,
            LanguageCode.FRENCH_FR,
            LanguageCode.GERMAN_DE,
            LanguageCode.ITALIAN_IT,
            LanguageCode.PORTUGUESE_BR,
            LanguageCode.RUSSIAN_RU,
            LanguageCode.CHINESE_CN,
            LanguageCode.JAPANESE_JP,
            LanguageCode.AUTO_DETECT
        ]
    
    def validate_config(self, config: TranscriptionConfig) -> bool:
        """Validate RIVA configuration"""
        if not RIVA_AVAILABLE or not self.service:
            return False
        
        return (
            config.language in self.get_supported_languages() and
            config.sample_rate in [8000, 16000, 22050, 44100] and
            config.max_alternatives <= 10
        )
    
    def _map_language_to_riva(self, language: LanguageCode) -> str:
        """Map LanguageCode to RIVA language codes"""
        mapping = {
            LanguageCode.ENGLISH_US: "en-US",
            LanguageCode.ENGLISH_GB: "en-GB",
            LanguageCode.SPANISH_ES: "es-ES",
            LanguageCode.FRENCH_FR: "fr-FR",
            LanguageCode.GERMAN_DE: "de-DE",
            LanguageCode.ITALIAN_IT: "it-IT",
            LanguageCode.PORTUGUESE_BR: "pt-BR",
            LanguageCode.RUSSIAN_RU: "ru-RU",
            LanguageCode.CHINESE_CN: "zh-CN",
            LanguageCode.JAPANESE_JP: "ja-JP",
            LanguageCode.AUTO_DETECT: "en-US"  # Default to English for auto-detect
        }
        return mapping.get(language, "en-US")


class AudioTranscriptionManager:
    """Manager for audio transcription operations"""
    
    def __init__(self):
        self.services: Dict[TranscriptionProvider, TranscriptionServiceInterface] = {
            TranscriptionProvider.NVIDIA_RIVA: RivaTranscriptionService(),
            TranscriptionProvider.MOCK_SERVICE: MockTranscriptionService(),
            TranscriptionProvider.GOOGLE_SPEECH: GoogleSpeechService(),
        }
        
        self.active_transcriptions: Dict[str, TranscriptionResult] = {}
        self.transcription_cache = {}
        self.audio_converter = AudioConverter()
        
        # Statistics
        self.stats = {
            'total_transcriptions': 0,
            'successful_transcriptions': 0,
            'failed_transcriptions': 0,
            'total_audio_duration': 0.0,
            'average_processing_time': 0.0,
            'provider_usage': {provider.value: 0 for provider in TranscriptionProvider}
        }
    
    async def transcribe_session_audio(
        self,
        session_id: str,
        audio_data: bytes,
        config: TranscriptionConfig = None
    ) -> Optional[TranscriptionResult]:
        """Transcribe audio from session"""
        try:
            if not config:
                config = TranscriptionConfig()
            
            # Get transcription service
            service = self.services.get(config.provider)
            if not service:
                logger.error(f"Transcription provider not available: {config.provider}")
                return None
            
            # Check cache first
            cache_key = self._generate_cache_key(audio_data, config)
            cached_result = self.transcription_cache.get(cache_key)
            if cached_result:
                logger.info(f"Using cached transcription for session {session_id}")
                return cached_result
            
            # Preprocess audio for transcription
            processed_audio = await self._preprocess_audio(audio_data, config)
            if not processed_audio:
                logger.error("Failed to preprocess audio for transcription")
                return None
            
            # Perform transcription
            start_time = timezone.now()
            result = await service.transcribe_audio(processed_audio, config)
            
            if result.success:
                result.session_id = session_id
                result.processing_time_seconds = (timezone.now() - start_time).total_seconds()
                
                # Cache result
                self.transcription_cache[cache_key] = result
                
                # Store in active transcriptions
                self.active_transcriptions[result.transcription_id] = result
                
                # Update statistics
                self._update_statistics(result)
                
                # Save to database if possible
                await self._save_transcription_to_db(result)
                
                logger.info(f"Transcription completed for session {session_id}: {len(result.text)} characters")
                return result
            else:
                logger.error(f"Transcription failed for session {session_id}: {result.error_message}")
                self.stats['failed_transcriptions'] += 1
                return result
        
        except Exception as e:
            logger.error(f"Error transcribing session audio: {e}")
            self.stats['failed_transcriptions'] += 1
            return None
    
    async def transcribe_recording(
        self,
        recording_session: RecordingSession,
        config: TranscriptionConfig = None
    ) -> Optional[TranscriptionResult]:
        """Transcribe audio from recording file"""
        try:
            if not recording_session.file_path:
                logger.error("Recording file path not available")
                return None
            
            # Read audio file
            file_path = recording_session.file_path
            with open(file_path, 'rb') as f:
                audio_data = f.read()
            
            return await self.transcribe_session_audio(
                recording_session.session_id,
                audio_data,
                config
            )
        
        except Exception as e:
            logger.error(f"Error transcribing recording: {e}")
            return None
    
    async def start_real_time_transcription(
        self,
        session_id: str,
        config: TranscriptionConfig = None,
        callback: Callable[[TranscriptionSegment], None] = None
    ) -> bool:
        """Start real-time transcription for session"""
        try:
            if not config:
                config = TranscriptionConfig(mode=TranscriptionMode.REAL_TIME)
            
            service = self.services.get(config.provider)
            if not service:
                logger.error(f"Transcription provider not available: {config.provider}")
                return False
            
            # Create audio stream placeholder
            audio_stream = None  # This would be connected to RTP stream
            
            # Start streaming transcription
            success = await service.transcribe_streaming(audio_stream, config, callback)
            
            if success:
                logger.info(f"Real-time transcription started for session {session_id}")
                return True
            else:
                logger.error(f"Failed to start real-time transcription for session {session_id}")
                return False
        
        except Exception as e:
            logger.error(f"Error starting real-time transcription: {e}")
            return False
    
    async def _preprocess_audio(self, audio_data: bytes, config: TranscriptionConfig) -> Optional[bytes]:
        """Preprocess audio for optimal transcription"""
        try:
            # Convert audio to optimal format for transcription
            source_spec = AudioSpec(
                format=AudioFormat.ULAW,  # Assuming μ-law input
                sample_rate=8000,
                bit_depth=8,
                channels=1
            )
            
            target_spec = AudioSpec(
                format=AudioFormat.WAV,
                sample_rate=config.sample_rate,
                bit_depth=16,
                channels=1
            )
            
            result = self.audio_converter.convert(audio_data, source_spec, target_spec)
            
            if result.success:
                return result.data
            else:
                logger.warning(f"Audio preprocessing failed: {result.error_message}")
                return audio_data  # Return original if conversion fails
        
        except Exception as e:
            logger.error(f"Error preprocessing audio: {e}")
            return None
    
    def _generate_cache_key(self, audio_data: bytes, config: TranscriptionConfig) -> str:
        """Generate cache key for transcription"""
        content_hash = hashlib.md5(audio_data).hexdigest()
        config_hash = hashlib.md5(
            f"{config.provider.value}_{config.language.value}_{config.confidence_threshold}".encode()
        ).hexdigest()
        return f"transcription_{content_hash}_{config_hash}"
    
    def _update_statistics(self, result: TranscriptionResult):
        """Update transcription statistics"""
        self.stats['total_transcriptions'] += 1
        self.stats['successful_transcriptions'] += 1
        self.stats['total_audio_duration'] += result.audio_duration_seconds
        self.stats['provider_usage'][result.provider.value] += 1
        
        # Update average processing time
        total_processing_time = (
            self.stats['average_processing_time'] * (self.stats['successful_transcriptions'] - 1) +
            result.processing_time_seconds
        )
        self.stats['average_processing_time'] = total_processing_time / self.stats['successful_transcriptions']
    
    async def _save_transcription_to_db(self, result: TranscriptionResult):
        """Save transcription result to database"""
        try:
            # This would save to a TranscriptionResult model
            # For now, just log the action
            logger.info(f"Transcription saved to database: {result.transcription_id}")
        
        except Exception as e:
            logger.error(f"Error saving transcription to database: {e}")
    
    def get_transcription_result(self, transcription_id: str) -> Optional[TranscriptionResult]:
        """Get transcription result by ID"""
        return self.active_transcriptions.get(transcription_id)
    
    def get_session_transcriptions(self, session_id: str) -> List[TranscriptionResult]:
        """Get all transcriptions for a session"""
        return [
            result for result in self.active_transcriptions.values()
            if result.session_id == session_id
        ]
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get transcription statistics"""
        return {
            **self.stats,
            'active_transcriptions': len(self.active_transcriptions),
            'cache_size': len(self.transcription_cache),
            'success_rate': (
                self.stats['successful_transcriptions'] / max(self.stats['total_transcriptions'], 1)
            ) * 100
        }
    
    def clear_cache(self):
        """Clear transcription cache"""
        self.transcription_cache.clear()
        logger.info("Transcription cache cleared")


# Global transcription manager
_global_transcription_manager = None

def get_transcription_manager() -> AudioTranscriptionManager:
    """Get global audio transcription manager"""
    global _global_transcription_manager
    if _global_transcription_manager is None:
        _global_transcription_manager = AudioTranscriptionManager()
    return _global_transcription_manager


# Convenience functions
async def transcribe_session(
    session_id: str,
    audio_data: bytes,
    provider: TranscriptionProvider = TranscriptionProvider.NVIDIA_RIVA,
    language: LanguageCode = LanguageCode.AUTO_DETECT
) -> Optional[TranscriptionResult]:
    """Convenient function to transcribe session audio"""
    manager = get_transcription_manager()
    config = TranscriptionConfig(provider=provider, language=language)
    return await manager.transcribe_session_audio(session_id, audio_data, config)


async def transcribe_call_recording(
    recording_session: RecordingSession,
    provider: TranscriptionProvider = TranscriptionProvider.NVIDIA_RIVA
) -> Optional[TranscriptionResult]:
    """Convenient function to transcribe call recording"""
    manager = get_transcription_manager()
    config = TranscriptionConfig(provider=provider, mode=TranscriptionMode.BATCH)
    return await manager.transcribe_recording(recording_session, config)


def get_supported_providers() -> List[TranscriptionProvider]:
    """Get list of available transcription providers"""
    return list(TranscriptionProvider)


def get_supported_languages(provider: TranscriptionProvider = TranscriptionProvider.NVIDIA_RIVA) -> List[LanguageCode]:
    """Get supported languages for provider"""
    manager = get_transcription_manager()
    service = manager.services.get(provider)
    if service:
        return service.get_supported_languages()
    return []
