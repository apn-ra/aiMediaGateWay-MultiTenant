# Author: RA
# Purpose: Audio Transcription
# Created: 27/09/2025
import hashlib
import json
import logging
import asyncio
import threading
from typing import Callable, List, Any
from django.conf import settings
from django.utils import timezone

from core.junie_codes.audio.audio_transcription import (TranscriptionProvider, TranscriptionResult,
                                                        TranscriptionConfig, LanguageCode)

logger = logging.getLogger(__name__)

import riva.client
from riva.client.proto import riva_asr_pb2

class AsyncToSyncStream:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.queue = asyncio.Queue()
        self.closed = False

    def put_nowait(self, item):
        # logger.debug("AsyncToSyncStream: put_nowait")
        asyncio.run_coroutine_threadsafe(self.queue.put(item), self.loop)

    def close(self):
        self.closed = True
        asyncio.run_coroutine_threadsafe(self.queue.put(None), self.loop)

    def start(self):
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def generator(self):
        while True:
            fut = asyncio.run_coroutine_threadsafe(self.queue.get(), self.loop)
            item = fut.result()  # blocking here, so sync
            if item is None:
                break
            yield item

class RivaTranscriptionService:
    """NVIDIA RIVA ASR service integration (for nvidia-riva-client>=2.19.0)"""

    def __init__(self):
        self.riva_uri = getattr(settings, "RIVA_ASR_URI", "localhost:50051")
        self.use_ssl = getattr(settings, "RIVA_USE_SSL", False)
        self.ssl_cert = getattr(settings, "RIVA_SSL_CERT", None)
        self.auth = None
        self.service = None
        try:
            self.auth = riva.client.Auth(
                uri=self.riva_uri,
                use_ssl=self.use_ssl,
                ssl_cert=self.ssl_cert,
            )
            self.service = riva.client.ASRService(auth=self.auth)
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
    """
    Manages audio transcription sessions and provides streaming transcription functionality.

    This class is used to manage the process of transcribing audio streams using a
    transcription service. It handles the setup, configuration, and control of streaming
    transcriptions. It supports functionalities like language mapping, speaker diarization,
    custom configuration, and word boosting. It also handles audio buffering and manages
    the lifecycle of a streaming audio session.

    :ivar callback: A function to handle transcription results.
    :type callback: Callable[[TranscriptionResult], None]
    :ivar session_id: Unique identifier for the transcription session.
    :type session_id: str
    :ivar bridge: An object for audio streaming bridge functionalities.
    :type bridge: AsyncToSyncStream
    :ivar config: Configuration settings for the transcription session.
    :type config: TranscriptionConfig
    :ivar streaming_config: Configuration for the streaming recognition service.
    :type streaming_config: riva.client.StreamingRecognitionConfig
    :ivar riva: RivaTranscriptionService instance for handling Riva-specific operations.
    :type riva: RivaTranscriptionService
    :ivar buffer: Internal buffer for handling audio data.
    :type buffer: Any
    :ivar frame_size: Size of each audio frame in bytes (default is 640).
    :type frame_size: int
    :ivar active: State flag indicating whether a transcription session is active.
    :type active: bool
    :ivar stats: Dictionary holding statistics about transcription sessions.
    :type stats: dict
    """
    def __init__(self):
        self.callback = None
        self.session_id = None
        self.bridge = None
        self.config = None
        self.streaming_config = None
        self.riva = RivaTranscriptionService()
        self.buffer = None
        self.frame_size = 640
        self.active = False

        # Statistics
        self.stats = {
            'total_transcriptions': 0,
            'successful_transcriptions': 0,
            'failed_transcriptions': 0,
            'total_audio_duration': 0.0,
            'average_processing_time': 0.0,
            'provider_usage': {provider.value: 0 for provider in TranscriptionProvider}
        }

    def start_stream(self, session_id:str, config: TranscriptionConfig, callback: Callable[[TranscriptionResult], None]) -> bool:
        """
        Starts a streaming transcription session for an audio stream.

        This method configures the streaming transcription by setting up language, model,
        and a variety of parameters like word boosting, speaker diarization, endpoint
        parameters, and custom configurations. It initiates an audio stream and starts
        a separate thread for processing the transcription. If the process is successful,
        it returns `True`. On failure, it logs the error and returns `False`.

        :param session_id: Unique identifier for the transcription session.
        :type session_id: str
        :param config: Configuration object containing settings such as language, model name,
                       and other transcription-specific parameters.
        :type config: TranscriptionConfig
        :param callback: Callable function to handle transcription results.
        :type callback: Callable[[TranscriptionResult], None]
        :return: Indicates whether the transcription session started successfully.
        :rtype: bool
        """
        
        try:
            self.callback = callback
            self.session_id = session_id
            self.bridge = AsyncToSyncStream()
            self.config = config
            encoding = riva_asr_pb2.RecognitionConfig.DESCRIPTOR.fields_by_name[
                "encoding"
            ].enum_type.values_by_name["LINEAR_PCM"].number

            self.streaming_config = riva.client.StreamingRecognitionConfig(
                config=riva.client.RecognitionConfig(
                    language_code= 'multi', #self.riva.map_language_to_riva(config.language),
                    model=config.model_name,
                    max_alternatives=1,
                    profanity_filter=config.filter_profanity,
                    enable_automatic_punctuation=config.punctuation,
                    verbatim_transcripts=False,
                    enable_word_time_offsets=config.word_time_offsets or config.speaker_diarization,
                    sample_rate_hertz=16000,
                    encoding=encoding,
                ),
                interim_results=True
            )

            riva.client.add_word_boosting_to_config(self.streaming_config, config.boosted_lm_words,
                                                    config.boosted_lm_score)
            riva.client.add_speaker_diarization_to_config(self.streaming_config, config.speaker_diarization,
                                                          config.diarization_max_speakers)
            riva.client.add_endpoint_parameters_to_config(
                self.streaming_config,
                config.endpoint_parameters.start_history,
                config.endpoint_parameters.start_threshold,
                config.endpoint_parameters.stop_history,
                config.endpoint_parameters.stop_history_eou,
                config.endpoint_parameters.stop_threshold,
                config.endpoint_parameters.stop_threshold_eou
            )

            custom_config = {
                "asr_confidence_threshold": config.confidence_threshold,
                "custom_domain": config.custom_domain,
            }

            riva.client.add_custom_configuration_to_config(
                self.streaming_config,
                json.dumps(custom_config)
            )

            self.bridge.start()
            threading.Thread(target=self.transcribe_streaming, daemon=True).start()

            logger.info(f"Audio stream started for session: {session_id}")
            self.active = True
            return True
        except Exception as e:
            logger.error(f"Error starting audio stream: {e}")
            self.active = False
            return False

    def stop_stream(self):
        self.active = False
        self.bridge.close()
        logger.info(f"Audio stream stopped for session: {self.session_id}")

    def transcribe_streaming(self):
        def audio_generator():
            try:
                for audio_chunk in self.bridge.generator():
                    original_frame = audio_chunk.payload
                    # original_frame = audio_chunk.original_packet.payload
                    # original_frame = audio_chunk

                    # frame_size = len(original_frame)  # bytes per frame
                    # bytes_per_sample = 2  # 16-bit = 2 bytes
                    # samples_per_frame = frame_size // bytes_per_sample
                    # duration_per_frame = samples_per_frame / 16000

                    #riva.client.sleep_audio_length(audio_chunk=original_frame, time_to_sleep=duration_per_frame)
                    # logger.debug(f"Frame Size: { len(original_frame) }")
                    yield original_frame
            except Exception as e:
                logger.error(f"Error in audio generator: {e}")
                raise

        riva.client.print_streaming(
            responses=self.riva.service.streaming_response_generator(
                audio_chunks=audio_generator(),
                streaming_config=self.streaming_config,
            ),
            show_intermediate=True,
            additional_info="time" if (self.config.word_time_offsets or self.config.speaker_diarization) else (
                "confidence" if self.config.print_confidence else "no"),
            word_time_offsets=self.config.word_time_offsets or self.config.speaker_diarization,
            speaker_diarization=self.config.speaker_diarization,
        )

        # for resp in self.riva.service.streaming_response_generator(
        #         audio_chunks=audio_generator(),
        #         streaming_config=self.streaming_config,
        #     ):
        #     for result in resp.results:
        #         if not result.alternatives:
        #             continue
        #         alt = result.alternatives[0]
        #         if result.is_final and alt.words:
        #             # logger.debug(f"Final Transcription Result: {result}")
        #             self.callback(TranscriptionResult(
        #                 transcription_id=self.session_id,
        #                 session_id=self.session_id,
        #                 provider=TranscriptionProvider.NVIDIA_RIVA,
        #                 text=result.alternatives[0].transcript,
        #                 language_detected='en-US',
        #                 confidence_average= result.alternatives[0].confidence,
        #                 segments= result.alternatives[0].words,
        #                 processing_time_seconds= 0.0,
        #                 audio_duration_seconds=0.0,
        #                 created_at=timezone.now(),
        #                 success=True,
        #             ))
