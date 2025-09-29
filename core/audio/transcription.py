# Author: RA
# Purpose: Audio Transcription
# Created: 27/09/2025

import asyncio
import logging
from typing import Callable

from django.conf import settings
from core.junie_codes.rtp_server import AudioFrame
from core.junie_codes.audio.audio_transcription import (TranscriptionProvider, TranscriptionSegment,
                                                        TranscriptionConfig, LanguageCode)

logger = logging.getLogger(__name__)

from riva.client.proto import riva_asr_pb2
from riva.client import (
    Auth,
    ASRService,
    RecognitionConfig,
    StreamingRecognitionConfig,
    AudioEncoding,
)

class RivaTranscriptionService:
    """NVIDIA RIVA ASR service integration (for nvidia-riva-client>=2.19.0)"""

    def __init__(self):
        self.riva_uri = getattr(settings, "RIVA_ASR_URI", "localhost:50051")
        self.use_ssl = getattr(settings, "RIVA_USE_SSL", False)
        self.ssl_cert = getattr(settings, "RIVA_SSL_CERT", None)
        self.auth = None
        self.service = None
        try:
            self.auth = Auth(
                uri=self.riva_uri,
                use_ssl=self.use_ssl,
                ssl_cert=self.ssl_cert,
            )
            self.service = ASRService(auth=self.auth)
            logger.info("RIVA ASR service initialized with URI: %s", self.riva_uri)
        except Exception as e:
            logger.error("Failed to initialize RIVA ASR service: %s", e)

    @staticmethod
    def map_language_to_riva(language: LanguageCode) -> str:
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
            LanguageCode.AUTO_DETECT: "en-US",
        }
        return mapping.get(language, "en-US")

class AudioTranscriptionManager:
    def __init__(self):
        self.riva = RivaTranscriptionService()
        self.buffer = None
        self.frame_size = 640
        self.active = False
        # Session state
        self.sessions = {}  # session_id -> {"queue": asyncio.Queue, "task": asyncio.Task, "active": bool}

        # Statistics
        self.stats = {
            'total_transcriptions': 0,
            'successful_transcriptions': 0,
            'failed_transcriptions': 0,
            'total_audio_duration': 0.0,
            'average_processing_time': 0.0,
            'provider_usage': {provider.value: 0 for provider in TranscriptionProvider}
        }

    async def start_transcription(self, session_id: str, config: TranscriptionConfig, callback: Callable):
        """Start a continuous streaming session."""
        if session_id in self.sessions:
            logger.warning(f"Session {session_id} already active")
            return

        self.buffer = bytearray()
        queue = asyncio.Queue()
        session = {"queue": queue, "task": None, "active": True}
        self.sessions[session_id] = session

        riva_cfg = RecognitionConfig(
            encoding=AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=config.sample_rate,
            language_code=self.riva.map_language_to_riva(config.language),
            enable_word_time_offsets=config.enable_word_timestamps,
            enable_speaker_diarization=config.enable_speaker_diarization,
            enable_automatic_punctuation=config.enable_punctuation,
        )

        streaming_config = StreamingRecognitionConfig(
            config=riva_cfg, interim_results=True
        )

        async def request_generator():
            # First, yield the streaming config
            yield streaming_config
            logger.debug(f"Streaming config sent for session {session_id}")

            # Continuously yield audio chunks as they arrive
            while session["active"]:
                chunk = await queue.get()  # waits for next audio frame
                if chunk is None:  # End-of-stream sentinel
                    logger.info(f"End of stream for session {session_id}")
                    break
                yield chunk  # Send chunk immediately to Riva

        async def run_stream():
            try:
                for resp in self.riva.service.streaming_response_generator(request_generator()):
                    for result in resp.results:
                        if not result.alternatives:
                            continue
                        alt = result.alternatives[0]
                        # Create a segment for interim/final transcripts
                        segment = TranscriptionSegment(
                            text=alt.transcript,
                            start_time_seconds=0.0,
                            end_time_seconds=0.0,
                            confidence=alt.confidence,
                            speaker_id=None,
                        )
                        callback(segment)

                        # Emit word-level timings for final results
                        if result.is_final and alt.words:
                            for w in alt.words:
                                callback(
                                    TranscriptionSegment(
                                        text=w.word,
                                        start_time_seconds=w.start_time.seconds + w.start_time.nanos / 1e9,
                                        end_time_seconds=w.end_time.seconds + w.end_time.nanos / 1e9,
                                        confidence=w.confidence,
                                        speaker_id=getattr(w, "speaker_tag", None),
                                    )
                                )
            except Exception as e:
                logger.error(f"Riva streaming error in session {session_id}: {e}")
            finally:
                logger.info(f"Streaming session {session_id} ended")
                session["active"] = False

        # Launch the streaming coroutine
        session["task"] = asyncio.create_task(run_stream())
        self.active = True

    @staticmethod
    def get_frame_duration(audioFrame: AudioFrame) -> float:
        bytes_per_ms = (audioFrame.sample_rate * audioFrame.channels * 2) / 1000
        return len(audioFrame.payload) / bytes_per_ms

    async def send_frame(self, session_id: str, audio_frame):
        """Push an audio frame to the session's queue."""
        session = self.sessions.get(session_id)
        if not session or not session["active"]:
            logger.error(f"Cannot send frame: session {session_id} not active")
            return

        self.buffer.extend(audio_frame.payload)
        while len(self.buffer) >= self.frame_size:
            frame = self.buffer[:self.frame_size]
            del self.buffer[:self.frame_size]
            request = riva_asr_pb2.StreamingRecognizeRequest(audio_content=frame)
            asyncio.create_task(session["queue"].put(request))

    async def stop_transcription(self, session_id: str):
        """Stop streaming and clean up session."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        if self.buffer:
            request = riva_asr_pb2.StreamingRecognizeRequest(audio_content=self.buffer)
            asyncio.create_task(session["queue"].put(request))
            self.buffer.clear()

        session["active"] = False
        await session["queue"].put(None)  # Sentinel to close generator
        if session["task"]:
            await session["task"]  # Wait for clean shutdown
        logger.info(f"Session {session_id} stopped")
        self.active = False
