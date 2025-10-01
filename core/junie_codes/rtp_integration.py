"""
RTP Server Integration with Session Manager

This module provides seamless integration between the RTP server and session management
system, ensuring synchronized lifecycle management and automatic resource cleanup.

Features:
- Automatic RTP endpoint creation/destruction based on session lifecycle
- Session synchronization with RTP statistics
- Event-driven integration between components
- Resource cleanup and error handling
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime
from django.utils import timezone

from core.audio.transcription import AudioTranscriptionManager
from core.session.manager import get_session_manager, CallSessionData
from core.junie_codes.rtp_server import (get_rtp_server, RTPEndpoint, RTPSessionEndpointManager,
                                         RTPStatisticsCollector, AudioFrame, AudioProcessor)
from core.junie_codes.audio.audio_transcription import TranscriptionConfig, \
    TranscriptionProvider, LanguageCode, TranscriptionResult

from core.junie_codes.audio.audio_conversion import get_audio_converter
from core.junie_codes.audio.audio_quality import get_quality_manager
from core.junie_codes.audio.audio_conversion import AudioSpec, AudioFormat

logger = logging.getLogger(__name__)


@dataclass
class RTPIntegrationConfig:
    """Configuration for RTP integration"""
    auto_create_endpoints: bool = True
    auto_cleanup_endpoints: bool = True
    enable_statistics: bool = True
    enable_quality_monitoring: bool = True
    sync_interval: int = 30  # seconds
    cleanup_delay: int = 60  # seconds before cleanup


class RTPSessionIntegrator:
    """
    Integration layer between RTP server and session manager
    
    Provides:
    - Automatic RTP endpoint lifecycle management
    - Session-RTP synchronization
    - Quality monitoring integration
    - Event-driven updates
    """
    
    def __init__(self, config: RTPIntegrationConfig = None):
        self.audio_processor = AudioProcessor()
        self.config = config or RTPIntegrationConfig()
        self.session_manager = get_session_manager()
        self.rtp_server = get_rtp_server()
        self.endpoint_manager = RTPSessionEndpointManager(self.rtp_server)
        self.statistics_collector = RTPStatisticsCollector()
        
        # Audio processing managers
        self.quality_manager = get_quality_manager()
        self.audio_converter = get_audio_converter()
        
        # Integration state
        self.active_integrations: Dict[str, Dict[str, Any]] = {}  # session_id -> integration data
        self.sync_task: Optional[asyncio.Task] = None
        self.cleanup_task: Optional[asyncio.Task] = None
        
        # Event handlers
        self.session_event_handlers: Dict[str, List[Callable]] = {
            'session_created': [],
            'session_updated': [],
            'session_ended': [],
            'session_error': []
        }
        
        # Audio buffering for transcription
        self.audio_buffers: Dict[str, List[AudioFrame]] = {}  # session_id -> audio frames
        self.buffer_locks: Dict[str, asyncio.Lock] = {}  # session_id -> lock for thread safety
        self.last_transcription_time: Dict[str, datetime] = {}  # session_id -> last transcription timestamp
        
        logger.info("RTP Session Integrator initialized")
    
    async def start(self):
        """Start the integration service"""
        try:
            # Start RTP components
            await self.rtp_server.start()
            await self.endpoint_manager.start()
            
            # Start integration tasks
            self.sync_task = asyncio.create_task(self._sync_loop())
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            
            logger.info("RTP Session Integrator started successfully")
            
        except Exception as e:
            logger.error(f"Error starting RTP Session Integrator: {e}")
            raise
    
    async def stop(self):
        """Stop the integration service"""
        try:
            # Cancel background tasks
            if self.sync_task:
                self.sync_task.cancel()
            if self.cleanup_task:
                self.cleanup_task.cancel()
            
            # Wait for tasks to complete
            tasks = [t for t in [self.sync_task, self.cleanup_task] if t and not t.cancelled()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            # Stop RTP components
            await self.endpoint_manager.stop()
            await self.rtp_server.stop()
            
            logger.info("RTP Session Integrator stopped")
            
        except Exception as e:
            logger.error(f"Error stopping RTP Session Integrator: {e}")

    def transcription_result(self, result: TranscriptionResult):
        # loop = asyncio.get_event_loop()
        # session = loop.run_until_complete(self.session_manager.get_session(result.session_id))
        logger.info(f"{result.session_id} TEXT:{result.text}")

    async def integrate_session(self, session_id: str, routing_decision=None) -> bool:
        """Integrate a session with RTP server"""
        try:
            # Get session data
            session = await self.session_manager.get_session(session_id)
            if not session:
                logger.error(f"Session {session_id} not found for RTP integration")
                return False
            
            # Check if already integrated
            if session_id in self.active_integrations:
                logger.debug(f"Session {session_id} already integrated with RTP")
                return True
            
            # Create RTP endpoint if configured
            endpoint = None
            if self.config.auto_create_endpoints:
                endpoint = await self._create_rtp_endpoint(session, routing_decision)
                if not endpoint:
                    logger.error(f"Failed to create RTP endpoint for session {session_id}")
                    return False
            
            # Create quality monitor if enabled
            quality_monitor = None
            if self.config.enable_quality_monitoring:
                quality_monitor = self.statistics_collector.create_quality_monitor(session_id)
            
            # Initialize audio processing components
            audio_quality_analyzer = None
            audio_streaming_registered = False
            audio_recording_started = False

            try:
                config = TranscriptionConfig(
                    word_time_offsets=False,
                    max_alternatives=1,
                    filter_profanity=True,
                    punctuation=False,
                    language=LanguageCode.ENGLISH_US,
                    speaker_diarization=False,
                    diarization_max_speakers=2,
                    model_name='conformer-en-US-asr-streaming-asr-bls-ensemble'
                )

                transcription_manager = AudioTranscriptionManager()
                transcription_manager.start_stream(
                    session_id=session_id,
                    config=config,
                    callback=self.transcription_result
                )

                # Initialize quality analyzer
                audio_quality_analyzer = self.quality_manager.create_analyzer(session_id)
                logger.info(f"Audio processing components initialized for session {session_id}")

            except Exception as e:
                logger.error(f"Error initializing audio processing components for session {session_id}: {e}")
            
            # Store integration data
            integration_data = {
                'session_id': session_id,
                'tenant_id': session.tenant_id,
                'endpoint': endpoint,
                'quality_monitor': quality_monitor,
                'audio_quality_analyzer': audio_quality_analyzer,
                'audio_streaming_registered': audio_streaming_registered,
                'audio_recording_started': audio_recording_started,
                'transcription_manager': transcription_manager,
                'created_at': timezone.now(),
                'last_sync': timezone.now(),
                'stats': {
                    'packets_processed': 0,
                    'bytes_processed': 0,
                    'errors': 0
                }
            }
            
            self.active_integrations[session_id] = integration_data
            
            # Register packet handlers for this session
            if endpoint:
                await self._register_packet_handlers(session_id, integration_data)
            
            # Update session with RTP information
            await self._update_session_with_rtp_info(session_id, endpoint)
            
            # Fire session integration event
            await self._fire_session_event('session_created', session_id, integration_data)
            
            logger.info(f"Successfully integrated session {session_id} with RTP server")
            return True
            
        except Exception as e:
            logger.error(f"Error integrating session {session_id} with RTP: {e}")
            return False
    
    async def de_integrate_session(self, session_id: str) -> bool:
        """Remove session integration with RTP server"""
        try:
            if session_id not in self.active_integrations:
                logger.debug(f"Session {session_id} not integrated with RTP")
                return True

            session = await self.session_manager.get_session(session_id)
            logger.info(f"Channel Name: {session.channel.name}")
            logger.info(f"Integration Status: {await self.get_integration_status(session_id)}\n\n")

            integration_data = self.active_integrations[session_id]
            
            # Remove RTP endpoint
            if integration_data.get('endpoint') and self.config.auto_cleanup_endpoints:
                await self.endpoint_manager.destroy_session_endpoint(session_id)
            
            # Remove quality monitor
            if integration_data.get('quality_monitor'):
                self.statistics_collector.remove_quality_monitor(session_id)

            # Remove Audio Quality Analyzer
            if integration_data.get('audio_quality_analyzer'):
                self.quality_manager.remove_analyzer(session_id)

            if integration_data.get('transcription_manager'):
                integration_data.get('transcription_manager').stop_stream()

            # Clean up integration data
            del self.active_integrations[session_id]
            
            # Fire session de-integration event
            await self._fire_session_event('session_ended', session_id, integration_data)

            # Shutdown Transcription Manager
            # await self.transcription_manager.stop_transcription(session_id)

            logger.info(f"Successfully de-integrated session {session_id} from RTP server")
            return True
            
        except Exception as e:
            logger.error(f"Error de-integrating session {session_id} from RTP: {e}")
            return False
    
    async def _create_rtp_endpoint(self, session: CallSessionData, routing_decision=None) -> Optional[RTPEndpoint]:
        """Create RTP endpoint for session"""
        try:
            # Determine codec from routing decision or use default
            codec = "slin16"  # Default codec
            if routing_decision and hasattr(routing_decision, 'metadata'):
                codec = routing_decision.metadata.get('preferred_codec', 'ulaw')

            remote_port = 5060  # Default SIP port, should be from session/routing
            
            # Create endpoint
            endpoint = await self.endpoint_manager.create_session_endpoint(
                session.session_id,
                session.tenant_id,
                session.asterisk_host,
                remote_port,
                codec
            )
            
            return endpoint
            
        except Exception as e:
            logger.error(f"Error creating RTP endpoint for session {session.session_id}: {e}")
            return None
    
    async def _register_packet_handlers(self, session_id: str, integration_data: Dict[str, Any]):
        """Register multiple packet handlers for comprehensive audio processing"""
        try:
            async def comprehensive_packet_handler(packet, endpoint):
                """Handle RTP packet for all audio processing functionalities"""
                try:

                    # Update integration stats
                    if endpoint.session_id in self.active_integrations:
                        stats = self.active_integrations[endpoint.session_id]['stats']
                        stats['packets_processed'] += 1
                        stats['bytes_processed'] += packet.size
                    
                        # Convert RTP packet to AudioFrame for audio processing
                        audio_frame = await self.audio_processor.process_packet(packet=packet, endpoint=endpoint, target_codec='slin16')
                        if not audio_frame:
                            return

                        # if audio_frame.is_speech and round(audio_frame.avg_speech_prob, 2) > 0.50:
                        #     logger.debug(f"Speech detected: {round(audio_frame.avg_speech_prob, 2)}")
                        transcript = self.active_integrations[endpoint.session_id].get('transcription_manager')
                        if transcript and round(audio_frame.avg_speech_prob, 2) > 0.50:
                            # logger.debug(f"Speech Probability: {round(audio_frame.avg_speech_prob, 2)}")
                            transcript.bridge.put_nowait(audio_frame)

                        # Quality Monitoring
                        quality_monitor = self.active_integrations[endpoint.session_id].get('quality_monitor')
                        if quality_monitor:
                            quality_monitor.update_with_packet(packet)


                    else:
                        logger.debug(f"Session ID: {endpoint.session_id} not on active integration")

                except Exception as e:
                    logger.error(f"Error in comprehensive packet handler for session {session_id}: {e}")
                    if session_id in self.active_integrations:
                        self.active_integrations[session_id]['stats']['errors'] += 1
            
            # Register the comprehensive handler with RTP server
            self.rtp_server.register_packet_handler(session_id=session_id, handler=comprehensive_packet_handler)
            logger.info(f"Registered comprehensive packet handler for session {session_id}")
            
        except Exception as e:
            logger.error(f"Error registering packet handlers for session {session_id}: {e}")

    @staticmethod
    def _convert_rtp_to_audio_frame(packet, endpoint) -> Optional[AudioFrame]:
        """Convert RTP packet to AudioFrame for audio processing"""
        try:
            # Create AudioFrame from RTP packet data
            audio_frame = AudioFrame(
                payload=packet.payload,
                timestamp=packet.header.timestamp,
                sequence_number=packet.header.sequence_number,
                processed_time=timezone.now(),
                codec=endpoint.codec,
                sample_rate=endpoint.sample_rate,
                channels=endpoint.channels,
                session_id=endpoint.session_id
            )
            return audio_frame
        except Exception as e:
            logger.error(f"Error converting RTP packet to AudioFrame: {e}")
            return None

    async def _buffer_and_chuck_audio_frame(self, session_id: str, audio_frame: AudioFrame, chuck_size:int = 3):
        try:
            if session_id in self.audio_buffers:
                self.audio_buffers[session_id] = []
                self.buffer_locks[session_id] = asyncio.Lock()
                self.last_transcription_time[session_id] = datetime.now()
                logger.debug(f"Initialized audio buffer for session {session_id}")

            # Acquire lock for thread-safe buffer operations
            async with self.buffer_locks[session_id]:
                # Add frame to buffer
                self.audio_buffers[session_id].append(audio_frame)

                # Check if we should trigger transcription
                current_time = datetime.now()
                time_since_last = (current_time - self.last_transcription_time[session_id]).total_seconds()
                buffer_size = len(self.audio_buffers[session_id])

                # Trigger transcription if buffer has enough frames or enough time has passed
                should_transcribe = (
                        buffer_size >= chuck_size or  # ~60ms at 20ms/frame
                        time_since_last >= 1.0  # Force transcription every 2 seconds
                )

                if should_transcribe and buffer_size > 0:
                    # Extract frames for transcription
                    # frames_to_process = self.audio_buffers[session_id].copy()
                    # self.audio_buffers[session_id].clear()

                    frames_to_process, self.audio_buffers[session_id] = (
                        self.audio_buffers[session_id],
                        []
                    )
                    self.last_transcription_time[session_id] = current_time

                    logger.debug(
                        f"Processing {len(frames_to_process)} audio frames for transcription in session {session_id}")


        except Exception as e:
            logger.error(f"Error buffering and chucking audio frame in session {session_id}: {e}")
    async def _process_audio_for_transcription(self, session_id: str, audio_frame: AudioFrame):
        """Process audio frame for real-time transcription"""
        try:
            # Initialize session-specific resources if not present
            if session_id not in self.audio_buffers:
                self.audio_buffers[session_id] = []
                self.buffer_locks[session_id] = asyncio.Lock()
                self.last_transcription_time[session_id] = datetime.now()
                logger.debug(f"Initialized audio buffer for session {session_id}")
            
            # Acquire lock for thread-safe buffer operations
            async with self.buffer_locks[session_id]:
                # Add frame to buffer
                self.audio_buffers[session_id].append(audio_frame)
                
                # Check if we should trigger transcription
                current_time = datetime.now()
                time_since_last = (current_time - self.last_transcription_time[session_id]).total_seconds()
                buffer_size = len(self.audio_buffers[session_id])
                
                # Trigger transcription if buffer has enough frames or enough time has passed
                should_transcribe = (
                    buffer_size >= 15 or  # ~300ms at 20ms/frame
                    time_since_last >= 1.0  # Force transcription every 2 seconds
                )
                
                if should_transcribe and buffer_size > 0:
                    # Extract frames for transcription
                    # frames_to_process = self.audio_buffers[session_id].copy()
                    # self.audio_buffers[session_id].clear()

                    frames_to_process, self.audio_buffers[session_id] = (
                        self.audio_buffers[session_id],
                        []
                    )
                    self.last_transcription_time[session_id] = current_time
                    
                    logger.debug(f"Processing {len(frames_to_process)} audio frames for transcription in session {session_id}")
                    
                    # Process frames in background to avoid blocking
                    asyncio.create_task(self._transcribe_audio_frames(session_id, frames_to_process))
                        
        except Exception as e:
            logger.error(f"Error processing audio for transcription in session {session_id}: {e}")
    
    async def _transcribe_audio_frames(self, session_id: str, frames: List[AudioFrame]):
        """Transcribe a batch of audio frames"""
        try:
            if not frames:
                return
            
            # Combine audio frames into a single audio stream
            combined_audio_data = await self._combine_audio_frames(frames)
            
            # Convert audio format for transcription (most services expect WAV/PCM)
            transcription_ready_audio = await self._convert_audio_for_transcription(combined_audio_data, frames[0])
            
            if not transcription_ready_audio:
                logger.warning(f"Failed to convert audio for transcription in session {session_id}")
                return
            
            # Configure transcription
            transcription_config = TranscriptionConfig(
                provider=TranscriptionProvider.NVIDIA_RIVA,  # Can be configured
                language=LanguageCode.AUTO_DETECT,
                enable_punctuation=True,
                enable_profanity_filter=False,
                model_name="citrinet-1024"  # RIVA specific
            )
            
            # Transcribe using the transcription manager
            result = await self.transcription_manager.transcribe_session_audio(
                session_id=session_id,
                audio_data=transcription_ready_audio,
                config=transcription_config
            )
            
            if result and result.segments:
                # Log successful transcription (in practice, you might want to emit events)
                total_text = " ".join([segment.text for segment in result.segments])
                logger.info(f"Transcription result for session {session_id}: '{total_text}'")
                
                # Could emit transcription events here for real-time display
                # await self._emit_transcription_event(session_id, result)
            else:
                logger.debug(f"No transcription result for session {session_id}")
                
        except Exception as e:
            logger.error(f"Error transcribing audio frames for session {session_id}: {e}")

    @staticmethod
    async def _combine_audio_frames(frames: List[AudioFrame]) -> bytes:
        """Combine multiple audio frames into a single audio stream"""
        try:
            # Sort frames by sequence number to ensure correct order
            sorted_frames = sorted(frames, key=lambda f: f.sequence_number)
            
            # Combine audio data
            combined_data = b''.join([frame.payload for frame in sorted_frames])
            
            logger.debug(f"Combined {len(sorted_frames)} frames into {len(combined_data)} bytes")
            return combined_data
            
        except Exception as e:
            logger.error(f"Error combining audio frames: {e}")
            return b''
    
    async def _convert_audio_for_transcription(self, audio_data: bytes, sample_frame: AudioFrame) -> Optional[bytes]:
        """Convert audio data to format suitable for transcription"""
        try:
            if not audio_data:
                return None
            
            # Determine source audio format based on codec
            codec_to_format = {
                'ulaw': AudioFormat.ULAW,
                'alaw': AudioFormat.ALAW,
                'slin16': AudioFormat.SLIN16,
                'pcm': AudioFormat.PCM_LINEAR,
                'linear': AudioFormat.PCM_LINEAR
            }
            
            source_format = codec_to_format.get(sample_frame.codec.lower(), AudioFormat.SLIN16)
            
            # Create source audio specification
            source_spec = AudioSpec(
                format=source_format,
                sample_rate=sample_frame.sample_rate or 8000,
                bit_depth=16,
                channels=sample_frame.channels or 1
            )
            
            # Create target specification (WAV format for transcription)
            target_spec = AudioSpec(
                format=AudioFormat.WAV,
                sample_rate=16000,  # Standard rate for transcription
                bit_depth=16,
                channels=1
            )
            
            # Convert audio format
            conversion_result = self.audio_converter.convert(
                data=audio_data,
                source_spec=source_spec,
                target_spec=target_spec,
                use_cache=True
            )
            
            if conversion_result and conversion_result.success:
                logger.debug(f"Successfully converted audio: {len(audio_data)} -> {len(conversion_result.data)} bytes")
                return conversion_result.data
            else:
                logger.error(f"Audio conversion failed: {conversion_result.error if conversion_result else 'Unknown error'}")
                return None
                
        except Exception as e:
            logger.error(f"Error converting audio for transcription: {e}")
            return None
    
    async def _update_session_with_rtp_info(self, session_id: str, endpoint: Optional[RTPEndpoint]):
        """Update session with RTP endpoint information"""
        try:
            if not endpoint:
                return
            
            session = await self.session_manager.get_session(session_id)
            if session:
                # Update session metadata with RTP info
                # logger.info(f"Session: {session}")
                session.rtp_endpoint_port = endpoint.local_port
                session.metadata.update({
                    'rtp_integration': {
                        'endpoint_port': endpoint.local_port,
                        'codec': endpoint.codec,
                        'integrated_at': timezone.now().isoformat()
                    }
                })
                
                await self.session_manager.update_session(session_id, session.to_dict())
                
        except Exception as e:
            logger.error(f"Error updating session {session_id} with RTP info: {e}")
    
    async def _sync_loop(self):
        """Periodic synchronization between session manager and RTP server"""
        try:
            while True:
                await asyncio.sleep(self.config.sync_interval)
                
                # Sync active sessions
                # await self._sync_active_sessions()
                
                # Update global statistics
                if self.config.enable_statistics:
                    self.statistics_collector.update_global_stats()
                    
        except asyncio.CancelledError:
            logger.info("RTP integration sync loop cancelled")
        except Exception as e:
            logger.error(f"Error in RTP integration sync loop: {e}")
    
    async def _cleanup_loop(self):
        """Periodic cleanup of stale integrations"""
        try:
            while True:
                await asyncio.sleep(self.config.cleanup_delay)
                
                # Find stale integrations
                current_time = timezone.now()
                stale_sessions = []
                
                for session_id, integration_data in self.active_integrations.items():
                    # Check if session still exists
                    session = await self.session_manager.get_session(session_id)
                    if not session or session.status in ['completed', 'failed', 'ended']:
                        stale_sessions.append(session_id)
                        continue
                    
                    # Check for inactive integrations
                    last_sync = integration_data.get('last_sync', integration_data['created_at'])
                    if (current_time - last_sync).total_seconds() > (self.config.cleanup_delay * 2):
                        logger.warning(f"Integration for session {session_id} appears stale")
                
                # Clean up stale sessions
                for session_id in stale_sessions:
                    logger.info(f"Cleaning up stale RTP integration for session {session_id}")
                    await self.de_integrate_session(session_id)
                
                if stale_sessions:
                    logger.info(f"Cleaned up {len(stale_sessions)} stale RTP integrations")
                    
        except asyncio.CancelledError:
            logger.info("RTP integration cleanup loop cancelled")
        except Exception as e:
            logger.error(f"Error in RTP integration cleanup loop: {e}")
    
    async def _sync_active_sessions(self):
        """Synchronize active sessions with RTP integrations"""
        try:
            # Get all active sessions
            active_sessions = await self.session_manager.get_tenant_sessions()
            
            # Check for sessions that should be integrated but aren't
            for session in active_sessions:
                if session.session_id not in self.active_integrations:
                    # Check if session needs RTP integration
                    if await self._should_integrate_session(session):
                        logger.info(f"Auto-integrating session {session.session_id} with RTP")
                        await self.integrate_session(session.session_id)
            
            # Update last sync time for existing integrations
            current_time = timezone.now()
            for integration_data in self.active_integrations.values():
                integration_data['last_sync'] = current_time
                
        except Exception as e:
            logger.error(f"Error syncing active sessions: {e}")
    
    async def _should_integrate_session(self, session: CallSessionData) -> bool:
        """Determine if a session should be integrated with RTP"""
        try:
            # Check session status
            if session.status not in ['answered', 'bridged', 'recording']:
                return False
            
            # Check if session has audio requirements
            metadata = session.metadata or {}
            routing_metadata = metadata.get('routing_metadata', {})
            
            # Sessions that require external media should be integrated
            if routing_metadata.get('external_media_required', True):
                return True
            
            # Sessions that require recording should be integrated
            if routing_metadata.get('recording_enabled', False):
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error determining RTP integration for session {session.session_id}: {e}")
            return False
    
    async def _fire_session_event(self, event_type: str, session_id: str, data: Dict[str, Any]):
        """Fire session event to registered handlers"""
        try:
            handlers = self.session_event_handlers.get(event_type, [])
            for handler in handlers:
                try:
                    await handler(session_id, data)
                except Exception as e:
                    logger.error(f"Error in session event handler {handler.__name__}: {e}")
                    
        except Exception as e:
            logger.error(f"Error firing session event {event_type}: {e}")
    
    def register_session_event_handler(self, event_type: str, handler: Callable):
        """Register handler for session events"""
        if event_type in self.session_event_handlers:
            self.session_event_handlers[event_type].append(handler)
            logger.info(f"Registered handler for event type: {event_type}")
        else:
            logger.warning(f"Unknown event type: {event_type}")
    
    async def get_integration_status(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get integration status for specific session"""
        integration_data = self.active_integrations.get(session_id)
        if not integration_data:
            return None
        
        # Get RTP statistics if available
        rtp_stats = {}
        if integration_data.get('quality_monitor'):
            quality_report = integration_data['quality_monitor'].get_quality_report()
            rtp_stats = quality_report
        
        return {
            'session_id': session_id,
            'tenant_id': integration_data['tenant_id'],
            'integrated_at': integration_data['created_at'].isoformat(),
            'last_sync': integration_data['last_sync'].isoformat(),
            'endpoint_active': integration_data.get('endpoint') is not None,
            'monitoring_enabled': integration_data.get('quality_monitor') is not None,
            'integration_stats': integration_data['stats'],
            'rtp_quality_stats': rtp_stats
        }
    
    async def get_system_status(self) -> Dict[str, Any]:
        """Get overall system integration status"""
        return {
            'timestamp': timezone.now().isoformat(),
            'active_integrations': len(self.active_integrations),
            'rtp_server_stats': self.rtp_server.get_statistics(),
            'statistics_collector_report': self.statistics_collector.get_system_report(),
            'integration_config': {
                'auto_create_endpoints': self.config.auto_create_endpoints,
                'auto_cleanup_endpoints': self.config.auto_cleanup_endpoints,
                'enable_statistics': self.config.enable_statistics,
                'enable_quality_monitoring': self.config.enable_quality_monitoring
            }
        }


# Global integrator instance
_rtp_integrator = None


def get_rtp_integrator() -> RTPSessionIntegrator:
    """Get global RTP session integrator instance"""
    global _rtp_integrator
    if _rtp_integrator is None:
        _rtp_integrator = RTPSessionIntegrator()
    return _rtp_integrator


async def cleanup_rtp_integrator():
    """Cleanup RTP integrator resources"""
    global _rtp_integrator
    if _rtp_integrator:
        await _rtp_integrator.stop()
        _rtp_integrator = None
        logger.info("RTP integrator cleaned up")
