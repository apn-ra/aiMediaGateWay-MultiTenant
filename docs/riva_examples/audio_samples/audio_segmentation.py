# Author: RA
# Purpose: 
# Created: 02/10/2025

from pydub import AudioSegment

# Load the MP3 file
mp3_file = "tagalog/sample2_mp3.mp3"
audio = AudioSegment.from_mp3(mp3_file)

# Segment duration in milliseconds (e.g., 60 seconds)
segment_duration = 60 * 1000

# Split audio into chunks
for i, start_ms in enumerate(range(0, len(audio), segment_duration)):
    end_ms = start_ms + segment_duration
    segment = audio[start_ms:end_ms]

    # Export segment to WAV
    segment.export(f"segment_{i + 1}.wav", format="wav")
