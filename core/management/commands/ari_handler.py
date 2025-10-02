# Author: RA
# Purpose: CLI Command ARI HANDLER
# Created: 22/09/2025

import asyncio
import logging
import soundfile as sf
import numpy as np

from django.core.management.base import BaseCommand
from core.audio.transcription import AudioTranscriptionManager
from core.junie_codes.audio.audio_conversion import detect_audio_format
from core.junie_codes.audio.audio_transcription import TranscriptionConfig, TranscriptionSegment, LanguageCode, \
    RivaEndpointParametersConfig, TranscriptionResult
from riva.client.argparse_utils import add_asr_config_argparse_parameters, add_connection_argparse_parameters

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Handle ARI events'

    def wav_to_slin16_frames(self, filepath: str, frame_duration_ms: int = 20):
        """
        Convert a WAV file to SLIN16 frames (16-bit PCM, 16kHz).

        Args:
            filepath: Path to the WAV file
            frame_duration_ms: Frame size in milliseconds (default 20ms)

        Returns:
            List of byte strings, each representing one SLIN16 frame
        """
        # Read audio file
        data, samplerate = sf.read(filepath, dtype="int16")

        # If stereo, convert to mono (take mean across channels)
        if data.ndim > 1:
            data = np.mean(data, axis=1).astype(np.int16)

        # Resample if needed
        if samplerate != 16000:
            import librosa
            data = librosa.resample(
                data.astype(np.float32), orig_sr=samplerate, target_sr=16000
            ).astype(np.int16)
            samplerate = 16000

        # Compute frame size (samples per frame)
        samples_per_frame = int(16000 * frame_duration_ms / 1000)

        # Split into frames
        frames = []
        for i in range(0, len(data), samples_per_frame):
            frame = data[i:i + samples_per_frame]
            if len(frame) < samples_per_frame:
                break  # Drop incomplete frame (optional: pad instead)
            frames.append(frame.tobytes())  # SLIN16 = raw PCM16 little-endian

        return frames

    def transcript_result(self, result: TranscriptionResult):
        logger.info(f"Transcript: {result.text}")

    def add_arguments(self, parser):
        # parser = argparse.ArgumentParser(
        #     description="Streaming transcription of a file via Riva AI Services. Streaming means that audio is sent to a "
        #                 "server in small chunks and transcripts are returned as soon as these transcripts are ready. "
        #                 "You may play transcribed audio simultaneously with transcribing by setting one of parameters "
        #                 "`--play-audio` or `--output-device`.",
        #     formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        # )

        parser.add_argument(
            "--file-streaming-chunk",
            type=int,
            default=1600,
            help="A maximum number of frames in one chunk sent to server.",
        )

        parser = add_connection_argparse_parameters(parser)
        parser = add_asr_config_argparse_parameters(parser, max_alternatives=True, profanity_filter=True,
                                                    word_time_offsets=True)
        # parser = parser.parse_args()

    async def traffic_generator(self, transcription):
        frames = self.wav_to_slin16_frames('/opt/apnra-files/aiMediaGatewayV2/docs/riva_examples/audio_samples/tagalog/segment_2.wav')

        await asyncio.sleep(1)
        #logger.info(f"Frames: {len(frames)}")

        for frame in frames:
            logger.debug(f"Frame: {detect_audio_format(frame)}")
            transcription.bridge.put_nowait(frame)
            await asyncio.sleep(0.02)

    async def handle_async(self, *args, **options):
        transcription = AudioTranscriptionManager()

        config = TranscriptionConfig(
            word_time_offsets=options.get('word_time_offsets', False),
            max_alternatives=options.get('max_alternatives', 1),
            filter_profanity=options.get('filter_profanity', False),
            punctuation= True, #options.get('punctuation', False),
            language=LanguageCode.ENGLISH_US,
            boosted_lm_words=options.get('boosted_lm_words', []),
            boosted_lm_score=options.get('boosted_lm_score', 4.0),
            speaker_diarization= False, #options.get('speaker_diarization', False),
            diarization_max_speakers= 2, #options.get('diarization_max_speakers', 2),
            endpoint_parameters=RivaEndpointParametersConfig(
                start_history=options.get('start_history', -1),
                start_threshold=options.get('start_threshold', -1.0),
                stop_history=options.get('stop_history', -1),
                stop_history_eou=options.get('stop_history_eou', -1),
                stop_threshold=options.get('stop_threshold', -1.0),
                stop_threshold_eou=options.get('stop_threshold_eou', -1.0),
            ),
            model_name= 'parakeet-1.1b-unified-ml-cs-universal-multi-asr-streaming-asr-bls-ensemble'
            # model_name='conformer-en-US-asr-streaming-asr-bls-ensemble'
        )


        transcription.start_stream(
            session_id='123456789',
            config=config,
            callback=self.transcript_result
        )

        await self.traffic_generator(transcription=transcription)

        await asyncio.sleep(10)

        transcription.stop_stream()

    def handle(self, *args, **options):
        asyncio.run(self.handle_async(*args, **options))
