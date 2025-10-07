"""
AudioRecording ViewSet for aiMediaGateway

This module contains ViewSets for the AudioRecording model with file handling
and tenant filtering capabilities.
"""

from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.http import HttpResponse, Http404
from django.db.models import Sum
from django.utils import timezone
import os
import mimetypes

from core.models import AudioRecording, UserProfile
from core.serializers import AudioRecordingSerializer, AudioTranscriptionSerializer
from core.junie_codes.permissions import TenantResourcePermission
from core.junie_codes.filters import AudioRecordingFilterSet


class AudioRecordingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing audio recordings with file handling.
    
    Provides CRUD operations for audio recording management with proper
    tenant isolation, file handling, and transcription capabilities.
    """
    
    queryset = AudioRecording.objects.all()
    serializer_class = AudioRecordingSerializer
    permission_classes = [IsAuthenticated, TenantResourcePermission]
    
    # Advanced filtering and search configuration
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_class = AudioRecordingFilterSet
    search_fields = [
        'filename',
        'call_session__caller_id',
        'call_session__dialed_number',
        'call_session__caller_name',
        'transcription_text',
    ]
    ordering_fields = ['created_at', 'duration_seconds', 'file_size', 'transcription_confidence']
    ordering = ['-created_at']
    
    def get_queryset(self):
        """
        Filter queryset based on tenant context and user permissions.
        
        Returns:
            QuerySet: Filtered audio recording queryset
        """
        queryset = super().get_queryset()
        
        # Add related data for efficiency
        queryset = queryset.select_related('call_session', 'call_session__tenant')
        
        # Filter by tenant context through call session
        if hasattr(self.request, 'tenant') and self.request.tenant:
            queryset = queryset.filter(call_session__tenant=self.request.tenant)
        else:
            # Fallback to user's tenant if no tenant context
            try:
                user_profile = UserProfile.objects.get(user=self.request.user)
                queryset = queryset.filter(call_session__tenant=user_profile.tenant)
            except UserProfile.DoesNotExist:
                return queryset.none()
        
        # Additional filtering based on user role
        if hasattr(self.request, 'user') and self.request.user.is_authenticated:
            try:
                user_profile = UserProfile.objects.get(user=self.request.user)
                
                # View-only users can see all recordings but with limited access
                # This is handled in the serializer's to_representation method
                
            except UserProfile.DoesNotExist:
                return queryset.none()
        
        return queryset
    
    def perform_create(self, serializer):
        """
        Create an audio recording with proper validation.
        
        Args:
            serializer: AudioRecordingSerializer instance
        """
        # Validate call session belongs to tenant
        call_session = serializer.validated_data['call_session']
        
        if hasattr(self.request, 'tenant') and self.request.tenant:
            if call_session.tenant != self.request.tenant:
                raise ValueError("Call session does not belong to current tenant")
        
        serializer.save()
    
    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        """
        Download audio recording file.
        
        Args:
            request: HTTP request
            pk: AudioRecording primary key
            
        Returns:
            HttpResponse: File download response
        """
        recording = self.get_object()
        
        if not recording.file_path or not os.path.exists(recording.file_path):
            raise Http404("Audio file not found")
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(recording.file_path)
        if not mime_type:
            mime_type = 'audio/wav'  # Default for audio files
        
        # Create response with file
        with open(recording.file_path, 'rb') as audio_file:
            response = HttpResponse(audio_file.read(), content_type=mime_type)
            response['Content-Disposition'] = f'attachment; filename="{recording.filename}"'
            response['Content-Length'] = recording.file_size
            
        return response
    
    @action(detail=True, methods=['get'])
    def stream(self, request, pk=None):
        """
        Stream audio recording file for in-browser playback.
        
        Args:
            request: HTTP request
            pk: AudioRecording primary key
            
        Returns:
            HttpResponse: Streaming audio response
        """
        recording = self.get_object()
        
        if not recording.file_path or not os.path.exists(recording.file_path):
            raise Http404("Audio file not found")
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(recording.file_path)
        if not mime_type:
            mime_type = 'audio/wav'
        
        # Handle range requests for audio streaming
        range_header = request.META.get('HTTP_RANGE', '').strip()
        range_match = None
        
        if range_header:
            import re
            range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        
        if range_match:
            # Partial content request
            start = int(range_match.group(1))
            end = range_match.group(2)
            end = int(end) if end else recording.file_size - 1
            
            with open(recording.file_path, 'rb') as audio_file:
                audio_file.seek(start)
                chunk_size = end - start + 1
                data = audio_file.read(chunk_size)
            
            response = HttpResponse(
                data,
                content_type=mime_type,
                status=206  # Partial Content
            )
            response['Content-Range'] = f'bytes {start}-{end}/{recording.file_size}'
            response['Accept-Ranges'] = 'bytes'
            response['Content-Length'] = chunk_size
        else:
            # Full file request
            with open(recording.file_path, 'rb') as audio_file:
                response = HttpResponse(audio_file.read(), content_type=mime_type)
                response['Content-Length'] = recording.file_size
        
        # Add caching headers for better performance
        response['Cache-Control'] = 'public, max-age=3600'
        
        return response
    
    @action(detail=True, methods=['post'])
    def transcribe(self, request, pk=None):
        """
        Start transcription for an audio recording.
        
        Args:
            request: HTTP request
            pk: AudioRecording primary key
            
        Returns:
            Response: Transcription status
        """
        recording = self.get_object()
        
        # Check if call session has transcription enabled
        if not recording.call_session.transcription_enabled:
            return Response(
                {'message': 'Transcription is not enabled for this session'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if transcription already exists
        if recording.transcription:
            return Response(
                {
                    'message': 'Recording is already transcribed',
                    'transcription_id': recording.id
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if audio file exists
        if not recording.file_path or not os.path.exists(recording.file_path):
            return Response(
                {'message': 'Audio file not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # TODO: Implement actual transcription logic
        # This would typically involve:
        # 1. Submitting the audio file to transcription service
        # 2. Storing job ID in metadata
        # 3. Setting up callback for completion
        
        # For now, simulate job submission
        if not recording.metadata:
            recording.metadata = {}
        recording.metadata['transcription_job_id'] = f"job_{recording.id}_{timezone.now().timestamp()}"
        recording.metadata['transcription_status'] = 'processing'
        recording.save()
        
        return Response({
            'message': 'Transcription job started',
            'job_id': recording.metadata['transcription_job_id'],
            'recording_id': recording.id
        })
    
    @action(detail=True, methods=['get', 'post'])
    def transcription(self, request, pk=None):
        """
        Get or update transcription for an audio recording.
        
        Args:
            request: HTTP request
            pk: AudioRecording primary key
            
        Returns:
            Response: Transcription data
        """
        recording = self.get_object()
        
        if request.method == 'GET':
            # Return current transcription data
            if not recording.transcription:
                return Response(
                    {'message': 'No transcription available'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            transcription_data = {
                'recording_id': recording.id,
                'transcription': recording.transcription,
                'language_code': recording.language_code,
                'confidence_score': recording.confidence_score,
                'word_count': len(recording.transcription.split()) if recording.transcription else 0,
                'created_at': recording.updated_at,
            }
            
            serializer = AudioTranscriptionSerializer(data=transcription_data)
            serializer.is_valid(raise_exception=True)
            return Response(serializer.data)
        
        else:  # POST - Update transcription
            serializer = AudioTranscriptionSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            
            validated_data = serializer.validated_data
            
            # Update recording with transcription data
            recording.transcription = validated_data['transcription']
            recording.language_code = validated_data.get('language_code')
            recording.confidence_score = validated_data.get('confidence_score')
            
            # Update metadata with additional transcription info
            if not recording.metadata:
                recording.metadata = {}
            
            recording.metadata.update({
                'transcription_status': 'completed',
                'transcription_completed_at': timezone.now().isoformat(),
                'word_count': validated_data.get('word_count'),
                'processing_time': validated_data.get('processing_time'),
            })
            
            if validated_data.get('segments'):
                recording.metadata['transcription_segments'] = validated_data['segments']
            
            recording.save()
            
            return Response({
                'message': 'Transcription updated successfully',
                'recording_id': recording.id
            })
    
    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """
        Get audio recording statistics for the current tenant.
        
        Args:
            request: HTTP request
            
        Returns:
            Response: Audio recording statistics
        """
        queryset = self.get_queryset()
        
        # Basic counts
        total_recordings = queryset.count()
        transcribed_recordings = queryset.filter(transcription__isnull=False).exclude(transcription='').count()
        
        # File size statistics
        size_stats = queryset.aggregate(
            total_size=Sum('file_size'),
            avg_size=Sum('file_size') / total_recordings if total_recordings > 0 else 0
        )
        
        # Format statistics
        format_stats = {}
        for fmt in ['wav', 'mp3', 'ogg', 'flac', 'aac']:
            format_stats[fmt] = queryset.filter(format=fmt).count()
        
        # Quality statistics
        quality_stats = {}
        for quality in ['low', 'medium', 'high', 'lossless']:
            quality_stats[quality] = queryset.filter(quality=quality).count()
        
        # Duration statistics (if available)
        duration_recordings = queryset.filter(duration__isnull=False)
        if duration_recordings.exists():
            durations = [r.duration for r in duration_recordings]
            avg_duration = sum(durations) / len(durations)
            total_duration = sum(durations)
        else:
            avg_duration = None
            total_duration = None
        
        stats = {
            'total_recordings': total_recordings,
            'transcribed_recordings': transcribed_recordings,
            'transcription_rate': (transcribed_recordings / total_recordings * 100) if total_recordings > 0 else 0,
            'total_size_bytes': size_stats['total_size'] or 0,
            'total_size_mb': round((size_stats['total_size'] or 0) / (1024 * 1024), 2),
            'average_size_bytes': size_stats['avg_size'] or 0,
            'average_size_mb': round((size_stats['avg_size'] or 0) / (1024 * 1024), 2),
            'average_duration_seconds': avg_duration,
            'total_duration_seconds': total_duration,
            'format_breakdown': format_stats,
            'quality_breakdown': quality_stats,
            'generated_at': timezone.now(),
        }
        
        return Response(stats)
    
    @action(detail=False, methods=['post'])
    def bulk_transcribe(self, request):
        """
        Start transcription for multiple recordings.
        
        Args:
            request: HTTP request with recording IDs
            
        Returns:
            Response: Bulk transcription status
        """
        recording_ids = request.data.get('recording_ids', [])
        
        if not recording_ids:
            return Response(
                {'message': 'No recording IDs provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Filter recordings by tenant and permissions
        queryset = self.get_queryset().filter(id__in=recording_ids)
        
        # Check which recordings can be transcribed
        eligible_recordings = []
        already_transcribed = []
        not_found = []
        no_transcription = []
        
        for recording_id in recording_ids:
            try:
                recording = queryset.get(id=recording_id)
                
                if not recording.call_session.transcription_enabled:
                    no_transcription.append(recording_id)
                elif recording.transcription:
                    already_transcribed.append(recording_id)
                else:
                    eligible_recordings.append(recording)
                    
            except AudioRecording.DoesNotExist:
                not_found.append(recording_id)
        
        # Start transcription for eligible recordings
        jobs_started = []
        for recording in eligible_recordings:
            if recording.file_path and os.path.exists(recording.file_path):
                # TODO: Implement actual transcription job submission
                if not recording.metadata:
                    recording.metadata = {}
                recording.metadata['transcription_job_id'] = f"job_{recording.id}_{timezone.now().timestamp()}"
                recording.metadata['transcription_status'] = 'processing'
                recording.save()
                
                jobs_started.append(recording.id)
        
        return Response({
            'message': f'Transcription started for {len(jobs_started)} recordings',
            'jobs_started': jobs_started,
            'already_transcribed': already_transcribed,
            'not_found': not_found,
            'no_transcription_enabled': no_transcription,
            'total_requested': len(recording_ids)
        })
