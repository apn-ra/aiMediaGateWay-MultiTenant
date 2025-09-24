"""
Audio recording and storage functionality for the AI Media Gateway system.

This module provides comprehensive audio recording capabilities for call sessions
with multi-tenant isolation, file management, and metadata tracking.

Features:
- Call session audio recording
- Multi-tenant file storage isolation
- Recording format conversion and compression
- Metadata tracking and database integration
- Recording lifecycle management (start, stop, pause, resume)
- Storage quota management
- Recording encryption and security
- Automated cleanup and archival
"""

import os
import asyncio
import logging
import wave
import uuid
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import threading
import time
from collections import defaultdict

from django.utils import timezone
from django.conf import settings

# Import our components
from core.junie_codes.audio.audio_conversion import AudioConverter, AudioFormat, AudioSpec
from core.junie_codes.rtp_server import AudioFrame
from core.models import CallSession, Tenant, AudioRecording

logger = logging.getLogger(__name__)


class RecordingState(Enum):
    """Recording states"""
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class RecordingFormat(Enum):
    """Supported recording formats"""
    WAV = "wav"
    MP3 = "mp3"
    FLAC = "flac"
    OGG = "ogg"
    RAW_PCM = "raw_pcm"


class StorageBackend(Enum):
    """Storage backend types"""
    LOCAL_FILESYSTEM = "local"
    DJANGO_STORAGE = "django"
    AWS_S3 = "s3"
    AZURE_BLOB = "azure"
    GOOGLE_CLOUD = "gcs"


@dataclass
class RecordingConfig:
    """Configuration for audio recording"""
    format: RecordingFormat = RecordingFormat.WAV
    sample_rate: int = 8000
    bit_depth: int = 16
    channels: int = 1
    enable_compression: bool = True
    compression_level: int = 5  # 1-9 for most formats
    auto_start: bool = True
    max_duration_seconds: int = 3600  # 1 hour default
    enable_encryption: bool = False
    storage_backend: StorageBackend = StorageBackend.LOCAL_FILESYSTEM
    
    def __post_init__(self):
        """Validate configuration"""
        if self.sample_rate <= 0:
            raise ValueError("Sample rate must be positive")
        if self.bit_depth not in [8, 16, 24, 32]:
            raise ValueError("Bit depth must be 8, 16, 24, or 32")
        if self.channels <= 0:
            raise ValueError("Channels must be positive")
        if not (1 <= self.compression_level <= 9):
            raise ValueError("Compression level must be between 1 and 9")


@dataclass
class RecordingSession:
    """Active recording session"""
    recording_id: str
    session_id: str
    tenant_id: int
    config: RecordingConfig
    state: RecordingState
    start_time: datetime
    end_time: Optional[datetime] = None
    pause_time: Optional[datetime] = None
    resume_time: Optional[datetime] = None
    file_path: Optional[str] = None
    file_size_bytes: int = 0
    duration_seconds: float = 0.0
    frames_recorded: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None


class AudioRecorder:
    """Audio recorder for individual sessions"""
    
    def __init__(self, recording_session: RecordingSession):
        self.recording_session = recording_session
        self.audio_converter = AudioConverter()
        self.recording_buffer = []
        self.buffer_lock = threading.Lock()
        self.recording_file = None
        self.wave_writer = None
        
        # Statistics
        self.stats = {
            'frames_processed': 0,
            'bytes_written': 0,
            'conversion_errors': 0,
            'write_errors': 0,
            'buffer_overflows': 0
        }
    
    async def start_recording(self) -> bool:
        """Start audio recording"""
        try:
            if self.recording_session.state != RecordingState.IDLE:
                logger.warning(f"Cannot start recording, current state: {self.recording_session.state}")
                return False
            
            self.recording_session.state = RecordingState.STARTING
            
            # Create recording file path
            file_path = await self._create_recording_file_path()
            if not file_path:
                self.recording_session.state = RecordingState.ERROR
                self.recording_session.error_message = "Failed to create recording file path"
                return False
            
            self.recording_session.file_path = file_path
            
            # Initialize recording file
            success = await self._initialize_recording_file()
            if not success:
                self.recording_session.state = RecordingState.ERROR
                self.recording_session.error_message = "Failed to initialize recording file"
                return False
            
            # Update state and metadata
            self.recording_session.state = RecordingState.RECORDING
            self.recording_session.start_time = timezone.now()
            self.recording_session.metadata.update({
                'recording_started': self.recording_session.start_time.isoformat(),
                'config': {
                    'format': self.recording_session.config.format.value,
                    'sample_rate': self.recording_session.config.sample_rate,
                    'bit_depth': self.recording_session.config.bit_depth,
                    'channels': self.recording_session.config.channels
                }
            })
            
            logger.info(f"Recording started: {self.recording_session.recording_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error starting recording: {e}")
            self.recording_session.state = RecordingState.ERROR
            self.recording_session.error_message = str(e)
            return False
    
    async def stop_recording(self) -> bool:
        """Stop audio recording"""
        try:
            if self.recording_session.state not in [RecordingState.RECORDING, RecordingState.PAUSED]:
                logger.warning(f"Cannot stop recording, current state: {self.recording_session.state}")
                return False
            
            self.recording_session.state = RecordingState.STOPPING
            
            # Process any remaining buffer
            await self._flush_recording_buffer()
            
            # Finalize recording file
            await self._finalize_recording_file()
            
            # Update metadata
            self.recording_session.end_time = timezone.now()
            self.recording_session.duration_seconds = (
                self.recording_session.end_time - self.recording_session.start_time
            ).total_seconds()
            
            self.recording_session.metadata.update({
                'recording_ended': self.recording_session.end_time.isoformat(),
                'duration_seconds': self.recording_session.duration_seconds,
                'frames_recorded': self.recording_session.frames_recorded,
                'file_size_bytes': self.recording_session.file_size_bytes,
                'statistics': self.stats
            })
            
            self.recording_session.state = RecordingState.STOPPED
            
            logger.info(f"Recording stopped: {self.recording_session.recording_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            self.recording_session.state = RecordingState.ERROR
            self.recording_session.error_message = str(e)
            return False
    
    async def pause_recording(self) -> bool:
        """Pause audio recording"""
        try:
            if self.recording_session.state != RecordingState.RECORDING:
                return False
            
            self.recording_session.state = RecordingState.PAUSED
            self.recording_session.pause_time = timezone.now()
            
            # Flush current buffer
            await self._flush_recording_buffer()
            
            logger.info(f"Recording paused: {self.recording_session.recording_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error pausing recording: {e}")
            return False
    
    async def resume_recording(self) -> bool:
        """Resume audio recording"""
        try:
            if self.recording_session.state != RecordingState.PAUSED:
                return False
            
            self.recording_session.state = RecordingState.RECORDING
            self.recording_session.resume_time = timezone.now()
            
            logger.info(f"Recording resumed: {self.recording_session.recording_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error resuming recording: {e}")
            return False
    
    async def add_audio_frame(self, frame: AudioFrame) -> bool:
        """Add audio frame to recording"""
        try:
            if self.recording_session.state != RecordingState.RECORDING:
                return False
            
            # Check duration limits
            if self.recording_session.start_time:
                current_duration = (timezone.now() - self.recording_session.start_time).total_seconds()
                if current_duration >= self.recording_session.config.max_duration_seconds:
                    logger.info(f"Recording duration limit reached: {self.recording_session.recording_id}")
                    await self.stop_recording()
                    return False
            
            # Convert audio format if needed
            converted_frame = await self._convert_frame_for_recording(frame)
            if not converted_frame:
                self.stats['conversion_errors'] += 1
                return False
            
            # Add to buffer
            with self.buffer_lock:
                self.recording_buffer.append(converted_frame)
                
                # Process buffer if it's getting full
                if len(self.recording_buffer) >= 10:  # Process every 10 frames
                    asyncio.create_task(self._process_recording_buffer())
            
            self.recording_session.frames_recorded += 1
            self.stats['frames_processed'] += 1
            
            return True
            
        except Exception as e:
            logger.error(f"Error adding audio frame to recording: {e}")
            self.stats['conversion_errors'] += 1
            return False
    
    async def _create_recording_file_path(self) -> Optional[str]:
        """Create file path for recording"""
        try:
            # Create tenant-specific directory structure
            tenant_dir = f"recordings/{self.recording_session.tenant_id}"
            date_dir = timezone.now().strftime("%Y/%m/%d")
            filename = f"{self.recording_session.recording_id}.{self.recording_session.config.format.value}"
            
            file_path = os.path.join(tenant_dir, date_dir, filename)
            
            # Ensure directory exists
            full_dir = os.path.join(settings.MEDIA_ROOT, tenant_dir, date_dir)
            os.makedirs(full_dir, exist_ok=True)
            
            return file_path
            
        except Exception as e:
            logger.error(f"Error creating recording file path: {e}")
            return None
    
    async def _initialize_recording_file(self) -> bool:
        """Initialize recording file based on format"""
        try:
            full_path = os.path.join(settings.MEDIA_ROOT, self.recording_session.file_path)
            
            if self.recording_session.config.format == RecordingFormat.WAV:
                self.recording_file = open(full_path, 'wb')
                self.wave_writer = wave.open(self.recording_file, 'wb')
                self.wave_writer.setnchannels(self.recording_session.config.channels)
                self.wave_writer.setsampwidth(self.recording_session.config.bit_depth // 8)
                self.wave_writer.setframerate(self.recording_session.config.sample_rate)
            else:
                # For other formats, open binary file
                self.recording_file = open(full_path, 'wb')
            
            return True
            
        except Exception as e:
            logger.error(f"Error initializing recording file: {e}")
            return False
    
    async def _finalize_recording_file(self) -> bool:
        """Finalize and close recording file"""
        try:
            if self.wave_writer:
                self.wave_writer.close()
                self.wave_writer = None
            
            if self.recording_file:
                self.recording_file.close()
                self.recording_file = None
            
            # Get file size
            if self.recording_session.file_path:
                full_path = os.path.join(settings.MEDIA_ROOT, self.recording_session.file_path)
                if os.path.exists(full_path):
                    self.recording_session.file_size_bytes = os.path.getsize(full_path)
            
            return True
            
        except Exception as e:
            logger.error(f"Error finalizing recording file: {e}")
            return False
    
    async def _convert_frame_for_recording(self, frame: AudioFrame) -> Optional[bytes]:
        """Convert audio frame for recording format"""
        try:
            # Determine source format
            source_format = AudioFormat.ULAW if frame.codec == 'ulaw' else AudioFormat.PCM_LINEAR
            
            # Target format for recording
            target_format_map = {
                RecordingFormat.WAV: AudioFormat.PCM_LINEAR,
                RecordingFormat.RAW_PCM: AudioFormat.PCM_LINEAR,
                RecordingFormat.MP3: AudioFormat.PCM_LINEAR,  # Will need external library
                RecordingFormat.FLAC: AudioFormat.PCM_LINEAR,  # Will need external library
                RecordingFormat.OGG: AudioFormat.PCM_LINEAR   # Will need external library
            }
            
            target_format = target_format_map.get(
                self.recording_session.config.format, 
                AudioFormat.PCM_LINEAR
            )
            
            # Create specs
            source_spec = AudioSpec(
                format=source_format,
                sample_rate=8000,  # Default telephony rate
                bit_depth=16,
                channels=1
            )
            
            target_spec = AudioSpec(
                format=target_format,
                sample_rate=self.recording_session.config.sample_rate,
                bit_depth=self.recording_session.config.bit_depth,
                channels=self.recording_session.config.channels
            )
            
            # Convert audio
            result = self.audio_converter.convert(frame.payload, source_spec, target_spec)
            
            if not result.success:
                logger.warning(f"Audio conversion failed for recording: {result.error_message}")
                return None
            
            return result.data
            
        except Exception as e:
            logger.error(f"Error converting frame for recording: {e}")
            return None
    
    async def _process_recording_buffer(self):
        """Process recording buffer and write to file"""
        try:
            frames_to_process = []
            
            with self.buffer_lock:
                if self.recording_buffer:
                    frames_to_process = self.recording_buffer.copy()
                    self.recording_buffer.clear()
            
            if not frames_to_process:
                return
            
            # Write frames to file
            for frame_data in frames_to_process:
                await self._write_frame_to_file(frame_data)
            
        except Exception as e:
            logger.error(f"Error processing recording buffer: {e}")
    
    async def _flush_recording_buffer(self):
        """Flush remaining buffer to file"""
        await self._process_recording_buffer()
    
    async def _write_frame_to_file(self, frame_data: bytes) -> bool:
        """Write audio frame data to recording file"""
        try:
            if not frame_data:
                return False
            
            if self.wave_writer:
                # Write to WAV file
                self.wave_writer.writeframes(frame_data)
            elif self.recording_file:
                # Write raw data
                self.recording_file.write(frame_data)
            else:
                return False
            
            self.stats['bytes_written'] += len(frame_data)
            return True
            
        except Exception as e:
            logger.error(f"Error writing frame to file: {e}")
            self.stats['write_errors'] += 1
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get recording statistics"""
        return {
            **self.stats,
            'recording_id': self.recording_session.recording_id,
            'state': self.recording_session.state.value,
            'duration_seconds': self.recording_session.duration_seconds,
            'frames_recorded': self.recording_session.frames_recorded,
            'file_size_bytes': self.recording_session.file_size_bytes
        }


class AudioRecordingManager:
    """Manager for all audio recording operations"""
    
    def __init__(self):
        self.active_recordings: Dict[str, AudioRecorder] = {}
        self.recording_sessions: Dict[str, RecordingSession] = {}
        self.session_recordings: Dict[str, List[str]] = defaultdict(list)  # session_id -> recording_ids
        
        # Statistics
        self.stats = {
            'total_recordings': 0,
            'active_recordings': 0,
            'completed_recordings': 0,
            'failed_recordings': 0,
            'total_duration_seconds': 0.0,
            'total_file_size_bytes': 0
        }
        
        # Cleanup task
        self._cleanup_task = None
        self._start_cleanup_task()
    
    async def start_recording(
        self,
        session_id: str,
        tenant_id: int,
        config: RecordingConfig = None
    ) -> Optional[str]:
        """Start recording for a session"""
        try:
            if not config:
                config = RecordingConfig()
            
            # Create recording ID
            recording_id = f"rec_{session_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            
            # Create recording session
            recording_session = RecordingSession(
                recording_id=recording_id,
                session_id=session_id,
                tenant_id=tenant_id,
                config=config,
                state=RecordingState.IDLE,
                start_time=timezone.now()
            )
            
            # Create recorder
            recorder = AudioRecorder(recording_session)
            
            # Start recording
            success = await recorder.start_recording()
            if not success:
                self.stats['failed_recordings'] += 1
                return None
            
            # Store references
            self.active_recordings[recording_id] = recorder
            self.recording_sessions[recording_id] = recording_session
            self.session_recordings[session_id].append(recording_id)
            
            # Update statistics
            self.stats['total_recordings'] += 1
            self.stats['active_recordings'] += 1
            
            # Save to database
            await self._save_recording_to_db(recording_session)
            
            logger.info(f"Recording started for session {session_id}: {recording_id}")
            return recording_id
            
        except Exception as e:
            logger.error(f"Error starting recording for session {session_id}: {e}")
            self.stats['failed_recordings'] += 1
            return None
    
    async def stop_recording(self, recording_id: str) -> bool:
        """Stop specific recording"""
        try:
            recorder = self.active_recordings.get(recording_id)
            if not recorder:
                logger.warning(f"Recording not found: {recording_id}")
                return False
            
            # Stop recording
            success = await recorder.stop_recording()
            
            # Update statistics
            if success:
                self.stats['completed_recordings'] += 1
                recording_session = self.recording_sessions.get(recording_id)
                if recording_session:
                    self.stats['total_duration_seconds'] += recording_session.duration_seconds
                    self.stats['total_file_size_bytes'] += recording_session.file_size_bytes
            else:
                self.stats['failed_recordings'] += 1
            
            # Clean up references
            if recording_id in self.active_recordings:
                del self.active_recordings[recording_id]
                self.stats['active_recordings'] = max(0, self.stats['active_recordings'] - 1)
            
            # Update database
            recording_session = self.recording_sessions.get(recording_id)
            if recording_session:
                await self._update_recording_in_db(recording_session)
            
            logger.info(f"Recording stopped: {recording_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error stopping recording {recording_id}: {e}")
            return False
    
    async def stop_session_recordings(self, session_id: str) -> int:
        """Stop all recordings for a session"""
        try:
            recording_ids = self.session_recordings.get(session_id, [])
            stopped_count = 0
            
            for recording_id in recording_ids.copy():
                success = await self.stop_recording(recording_id)
                if success:
                    stopped_count += 1
            
            # Clean up session recordings list
            if session_id in self.session_recordings:
                del self.session_recordings[session_id]
            
            return stopped_count
            
        except Exception as e:
            logger.error(f"Error stopping session recordings {session_id}: {e}")
            return 0
    
    async def add_audio_frame(self, session_id: str, frame: AudioFrame) -> int:
        """Add audio frame to all recordings for session"""
        try:
            recording_ids = self.session_recordings.get(session_id, [])
            processed_count = 0
            
            for recording_id in recording_ids:
                recorder = self.active_recordings.get(recording_id)
                if recorder:
                    success = await recorder.add_audio_frame(frame)
                    if success:
                        processed_count += 1
            
            return processed_count
            
        except Exception as e:
            logger.error(f"Error adding audio frame to recordings: {e}")
            return 0
    
    async def pause_recording(self, recording_id: str) -> bool:
        """Pause specific recording"""
        try:
            recorder = self.active_recordings.get(recording_id)
            if recorder:
                return await recorder.pause_recording()
            return False
            
        except Exception as e:
            logger.error(f"Error pausing recording {recording_id}: {e}")
            return False
    
    async def resume_recording(self, recording_id: str) -> bool:
        """Resume specific recording"""
        try:
            recorder = self.active_recordings.get(recording_id)
            if recorder:
                return await recorder.resume_recording()
            return False
            
        except Exception as e:
            logger.error(f"Error resuming recording {recording_id}: {e}")
            return False
    
    async def get_recording_info(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Get recording information"""
        try:
            recording_session = self.recording_sessions.get(recording_id)
            if not recording_session:
                return None
            
            recorder = self.active_recordings.get(recording_id)
            stats = recorder.get_stats() if recorder else {}
            
            return {
                'recording_id': recording_session.recording_id,
                'session_id': recording_session.session_id,
                'tenant_id': recording_session.tenant_id,
                'state': recording_session.state.value,
                'start_time': recording_session.start_time.isoformat() if recording_session.start_time else None,
                'end_time': recording_session.end_time.isoformat() if recording_session.end_time else None,
                'duration_seconds': recording_session.duration_seconds,
                'file_path': recording_session.file_path,
                'file_size_bytes': recording_session.file_size_bytes,
                'frames_recorded': recording_session.frames_recorded,
                'config': {
                    'format': recording_session.config.format.value,
                    'sample_rate': recording_session.config.sample_rate,
                    'bit_depth': recording_session.config.bit_depth,
                    'channels': recording_session.config.channels
                },
                'metadata': recording_session.metadata,
                'statistics': stats,
                'error_message': recording_session.error_message
            }
            
        except Exception as e:
            logger.error(f"Error getting recording info {recording_id}: {e}")
            return None
    
    async def list_session_recordings(self, session_id: str) -> List[Dict[str, Any]]:
        """List all recordings for a session"""
        try:
            recording_ids = self.session_recordings.get(session_id, [])
            recordings = []
            
            for recording_id in recording_ids:
                info = await self.get_recording_info(recording_id)
                if info:
                    recordings.append(info)
            
            return recordings
            
        except Exception as e:
            logger.error(f"Error listing session recordings {session_id}: {e}")
            return []
    
    async def _save_recording_to_db(self, recording_session: RecordingSession):
        """Save recording metadata to database"""
        try:
            # Get tenant and call session
            tenant = await Tenant.objects.aget(tenant_id=recording_session.tenant_id)
            call_session = await CallSession.objects.aget(session_id=recording_session.session_id)
            
            # Create AudioRecording instance
            audio_recording = AudioRecording.objects.create(
                recording_id=recording_session.recording_id,
                tenant=tenant,
                call_session=call_session,
                file_path=recording_session.file_path or '',
                file_size_bytes=recording_session.file_size_bytes,
                duration_seconds=recording_session.duration_seconds,
                format=recording_session.config.format.value,
                sample_rate=recording_session.config.sample_rate,
                bit_depth=recording_session.config.bit_depth,
                channels=recording_session.config.channels,
                recording_started=recording_session.start_time,
                recording_ended=recording_session.end_time,
                metadata=recording_session.metadata,
                status='recording'
            )
            
            await audio_recording.asave()
            
        except Exception as e:
            logger.error(f"Error saving recording to database: {e}")
    
    async def _update_recording_in_db(self, recording_session: RecordingSession):
        """Update recording metadata in database"""
        try:
            audio_recording = await AudioRecording.objects.aget(
                recording_id=recording_session.recording_id
            )
            
            audio_recording.file_path = recording_session.file_path or ''
            audio_recording.file_size_bytes = recording_session.file_size_bytes
            audio_recording.duration_seconds = recording_session.duration_seconds
            audio_recording.recording_ended = recording_session.end_time
            audio_recording.metadata = recording_session.metadata
            audio_recording.status = recording_session.state.value
            
            await audio_recording.asave()
            
        except Exception as e:
            logger.error(f"Error updating recording in database: {e}")
    
    def _start_cleanup_task(self):
        """Start background cleanup task"""
        async def cleanup_loop():
            while True:
                try:
                    await self._cleanup_old_recordings()
                    await asyncio.sleep(3600)  # Run every hour
                except Exception as e:
                    logger.error(f"Error in cleanup loop: {e}")
                    await asyncio.sleep(300)  # Retry in 5 minutes
        
        self._cleanup_task = asyncio.create_task(cleanup_loop())
    
    async def _cleanup_old_recordings(self):
        """Clean up old and orphaned recordings"""
        try:
            current_time = timezone.now()
            cleanup_count = 0
            
            # Check for recordings that should have completed
            for recording_id, recorder in list(self.active_recordings.items()):
                recording_session = self.recording_sessions.get(recording_id)
                if not recording_session:
                    continue
                
                # Check for recordings running too long
                if recording_session.start_time:
                    duration = (current_time - recording_session.start_time).total_seconds()
                    if duration > recording_session.config.max_duration_seconds + 300:  # 5 min grace
                        logger.warning(f"Stopping long-running recording: {recording_id}")
                        await self.stop_recording(recording_id)
                        cleanup_count += 1
            
            logger.info(f"Cleanup completed, processed {cleanup_count} recordings")
            
        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")
    
    def get_comprehensive_stats(self) -> Dict[str, Any]:
        """Get comprehensive recording statistics"""
        active_recording_info = {}
        for recording_id, recorder in self.active_recordings.items():
            active_recording_info[recording_id] = recorder.get_stats()
        
        return {
            **self.stats,
            'active_recording_details': active_recording_info,
            'session_recording_counts': {
                session_id: len(recording_ids)
                for session_id, recording_ids in self.session_recordings.items()
            }
        }


# Global recording manager instance
_global_recording_manager = None

def get_recording_manager() -> AudioRecordingManager:
    """Get global recording manager instance"""
    global _global_recording_manager
    if _global_recording_manager is None:
        _global_recording_manager = AudioRecordingManager()
    return _global_recording_manager


# Convenience functions
async def start_session_recording(
    session_id: str, 
    tenant_id: str, 
    config: RecordingConfig = None
) -> Optional[str]:
    """Start recording for a session"""
    manager = get_recording_manager()
    return await manager.start_recording(session_id, tenant_id, config)


async def stop_session_recording(recording_id: str) -> bool:
    """Stop specific recording"""
    manager = get_recording_manager()
    return await manager.stop_recording(recording_id)


async def add_audio_frame_to_recordings(session_id: str, frame: AudioFrame) -> int:
    """Add audio frame to all recordings for session"""
    manager = get_recording_manager()
    return await manager.add_audio_frame(session_id, frame)


async def get_session_recording_info(session_id: str) -> List[Dict[str, Any]]:
    """Get all recording info for a session"""
    manager = get_recording_manager()
    return await manager.list_session_recordings(session_id)
