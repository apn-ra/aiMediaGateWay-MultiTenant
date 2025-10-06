# Author: RA
# Purpose: 
# Created: 30/09/2025

# SPDX-FileCopyrightText: Copyright (c) 2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

import asyncio
import queue
import threading
import argparse
import os
import logging
import soundfile as sf
import riva.client
from riva.client.proto import riva_asr_pb2
from riva.client.argparse_utils import (
    add_asr_config_argparse_parameters,
    add_connection_argparse_parameters,
)

logger = logging.getLogger(__name__)

class AsyncGeneratorIterator:
    """
    Convert an async generator to a synchronous iterator for Riva.
    """
    def __init__(self, async_gen):
        self._async_gen = async_gen
        self._loop = asyncio.new_event_loop()
        self._agen = self._async_gen.__aiter__()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return self._loop.run_until_complete(self._agen.__anext__())
        except StopAsyncIteration:
            self._loop.close()
            raise StopIteration

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Streaming transcription of a file via Riva AI Services using soundfile/numpy.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input-file", help="A path to a local file to stream.")
    group.add_argument("--list-models", action="store_true", help="List available models.")
    group.add_argument("--list-devices", action="store_true", help="List output devices indices")

    parser.add_argument(
        "--show-intermediate", action="store_true", help="Show intermediate transcripts as they are available."
    )
    parser.add_argument(
        "--output-device",
        type=int,
        default=None,
        help="Output audio device index for simultaneous playback.",
    )
    parser.add_argument(
        "--play-audio",
        action="store_true",
        help="Play input audio simultaneously with transcribing.",
    )
    parser.add_argument(
        "--file-streaming-chunk",
        type=int,
        default=1600,
        help="Maximum number of frames in one chunk sent to server.",
    )
    parser.add_argument(
        "--simulate-realtime",
        action="store_true",
        help="Send audio at real-time speed instead of as fast as possible.",
    )
    parser.add_argument(
        "--print-confidence",
        action="store_true",
        help="Print transcript stability and confidence.",
    )
    parser = add_connection_argparse_parameters(parser)
    parser = add_asr_config_argparse_parameters(
        parser, max_alternatives=True, profanity_filter=True, word_time_offsets=True
    )
    args = parser.parse_args()
    if args.play_audio or args.output_device is not None or args.list_devices:
        import riva.client.audio_io
    return args

async def file_packet_producer(file_path, chunk_frames=320, simulate_ms=200):
    """
    Async generator that yields accumulated chunks for Riva.
    """
    bytes_per_sample = 2
    with sf.SoundFile(file_path, "r") as f:
        samplerate = f.samplerate
        channels = f.channels
        buffer = bytearray()
        target_bytes = int(simulate_ms / 1000 * samplerate * channels * bytes_per_sample)

        while True:
            data = f.read(frames=chunk_frames, dtype="int16", always_2d=True)
            if len(data) == 0:
                break
            if channels > 1:
                data = data.reshape(-1)
            buffer.extend(data.tobytes())

            if len(buffer) >= target_bytes:
                yield bytes(buffer)
                buffer.clear()

            await asyncio.sleep(chunk_frames / samplerate)  # simulate real-time

        if buffer:
            yield bytes(buffer)  # sentinel to stop


async def main() -> None:
    async_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    args = parse_args()
    if args.list_devices:
        riva.client.audio_io.list_output_devices()
        return

    auth = riva.client.Auth(args.ssl_cert, args.use_ssl, args.server, args.metadata)
    asr_service = riva.client.ASRService(auth)

    if args.list_models:
        asr_models = dict()
        config_response = asr_service.stub.GetRivaSpeechRecognitionConfig(
            riva.client.proto.riva_asr_pb2.RivaSpeechRecognitionConfigRequest()
        )
        for model_config in config_response.model_config:
            if model_config.parameters["type"] == "online":
                language_code = model_config.parameters["language_code"]
                model = {"model": [model_config.model_name]}
                if language_code in asr_models:
                    asr_models[language_code].append(model)
                else:
                    asr_models[language_code] = [model]

        print("Available ASR models")
        asr_models = dict(sorted(asr_models.items()))
        print(asr_models)
        return

    if not os.path.isfile(args.input_file):
        print(f"Invalid input file path: {args.input_file}")
        return

    encoding = riva_asr_pb2.RecognitionConfig.DESCRIPTOR.fields_by_name[
        "encoding"
    ].enum_type.values_by_name["LINEAR_PCM"].number

    config = riva.client.StreamingRecognitionConfig(
        config=riva.client.RecognitionConfig(
            language_code=args.language_code,
            model=args.model_name,
            max_alternatives=1,
            profanity_filter=args.profanity_filter,
            enable_automatic_punctuation=args.automatic_punctuation,
            verbatim_transcripts=not args.no_verbatim_transcripts,
            enable_word_time_offsets=args.word_time_offsets or args.speaker_diarization,
            sample_rate_hertz=16000,
            encoding=encoding,
        ),
        interim_results=True,
    )

    riva.client.add_word_boosting_to_config(
        config, args.boosted_lm_words, args.boosted_lm_score
    )
    riva.client.add_speaker_diarization_to_config(
        config, args.speaker_diarization, args.diarization_max_speakers
    )
    riva.client.add_endpoint_parameters_to_config(
        config,
        args.start_history,
        args.start_threshold,
        args.stop_history,
        args.stop_history_eou,
        args.stop_threshold,
        args.stop_threshold_eou,
    )
    riva.client.add_custom_configuration_to_config(config, args.custom_configuration)

    sound_callback = None
    try:
        if args.play_audio or args.output_device is not None:
            wp = riva.client.get_wav_file_parameters(args.input_file)
            sound_callback = riva.client.audio_io.SoundCallBack(
                args.output_device,
                wp["sampwidth"],
                wp["nchannels"],
                wp["framerate"],
            )
            delay_callback = sound_callback
        else:
            delay_callback = (
                riva.client.sleep_audio_length if args.simulate_realtime else None
            )

        async_gen = file_packet_producer("/opt/apnra-files/aiMediaGatewayV2/docs/riva_examples/sample1.wav", chunk_frames=320, simulate_ms=200)

        audio_gen = AsyncGeneratorIterator(async_gen)

        for audio_chunk in audio_gen:
            logger.info(f"Audio chunk: {len(audio_chunk)}")

        # Bridge async queue → sync iterator
        # audio_gen = AsyncQueueBridge(
        #     async_queue=async_queue, loop=loop
        # )
        #
        # riva.client.print_streaming(
        #     responses=asr_service.streaming_response_generator(
        #         audio_chunks=audio_gen,
        #         streaming_config=config,
        #     ),
        #     show_intermediate=args.show_intermediate,
        #     additional_info="time"
        #     if (args.word_time_offsets or args.speaker_diarization)
        #     else ("confidence" if args.print_confidence else "no"),
        #     word_time_offsets=args.word_time_offsets or args.speaker_diarization,
        #     speaker_diarization=args.speaker_diarization,
        # )
    finally:
        if sound_callback is not None and sound_callback.opened:
            sound_callback.close()


if __name__ == "__main__":
    asyncio.run(main())
