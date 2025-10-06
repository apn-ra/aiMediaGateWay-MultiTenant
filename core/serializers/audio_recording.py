"""
AudioRecording serializers for aiMediaGateway

This module contains serializers for the AudioRecording model with conditional
transcription data and file handling capabilities.
"""

from rest_framework import serializers
from django.core.files.base import ContentFile
from django.utils import timezone
import os
import mimetypes
from core.models import AudioRecording, CallSession


class AudioRecordingSerializer(serializers.ModelSerializer):
    """
    Serializer for AudioRecording model with conditional transcription data.
    
    Provides audio recording information with optional transcription data
    and file handling capabilities.
    """
    
    # Read-only fields
    id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    
    # Related session information
    session_caller_id = serializers.CharField(source='call_session.caller_id', read_only=True)
    session_dialed_number = serializers.CharField(source='call_session.dialed_number', read_only=True)
    session_start_time = serializers.DateTimeField(source='call_session.start_time', read_only=True)
    
    # Computed fields
    file_size_mb = serializers.SerializerMethodField()
    duration_formatted = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    transcription_status = serializers.SerializerMethodField()
    
    class Meta:
        model = AudioRecording
        fields = [
            'id',
            'call_session',
            'session_caller_id',
            'session_dialed_number',
            'session_start_time',
            'filename',
            'file_path',
            'file_size',
            'file_size_mb',
            'duration',
            'duration_formatted',
            'format',
            'quality',
            'channels',
            'sample_rate',
            'bit_rate',
            'transcription',
            'transcription_status',
            'language_code',
            'confidence_score',
            'download_url',
            'metadata',
            'created_at',
            'updated_at',
        ]
        
        extra_kwargs = {
            'call_session': {'read_only': True},  # Set by context or creation logic
            'file_path': {'read_only': True},     # Generated automatically
            'file_size': {'read_only': True},     # Calculated from file
        }
    
    def get_file_size_mb(self, obj):
        """
        Get file size in megabytes.
        
        Args:
            obj: AudioRecording instance
            
        Returns:
            float: File size in MB rounded to 2 decimal places
        """
        if obj.file_size:
            return round(obj.file_size / (1024 * 1024), 2)
        return None
    
    def get_duration_formatted(self, obj):
        """
        Get formatted duration string (HH:MM:SS).
        
        Args:
            obj: AudioRecording instance
            
        Returns:
            str: Formatted duration string
        """
        if obj.duration:
            hours = int(obj.duration // 3600)
            minutes = int((obj.duration % 3600) // 60)
            seconds = int(obj.duration % 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return None
    
    def get_download_url(self, obj):
        """
        Get download URL for the audio file.
        
        Args:
            obj: AudioRecording instance
            
        Returns:
            str: Download URL or None if file not accessible
        """
        request = self.context.get('request')
        if request and obj.file_path:
            # Generate API endpoint URL for file download
            return request.build_absolute_uri(
                f"/api/v1/audio-recordings/{obj.id}/download/"
            )
        return None
    
    def get_transcription_status(self, obj):
        """
        Get transcription status based on available data.
        
        Args:
            obj: AudioRecording instance
            
        Returns:
            str: Transcription status
        """
        if obj.transcription:
            return 'completed'
        elif obj.call_session.transcription_enabled:
            # Check if transcription is in progress (from metadata)
            if obj.metadata and obj.metadata.get('transcription_job_id'):
                return 'processing'
            return 'pending'
        return 'disabled'
    
    def validate_format(self, value):
        """
        Validate audio format.
        
        Args:
            value: Audio format to validate
            
        Returns:
            str: Validated format
        """
        if value:
            valid_formats = ['wav', 'mp3', 'ogg', 'flac', 'aac', 'm4a']
            if value.lower() not in valid_formats:
                raise serializers.ValidationError(
                    f"Invalid audio format. Supported formats: {', '.join(valid_formats)}"
                )
        return value.lower() if value else value
    
    def validate_quality(self, value):
        """
        Validate audio quality setting.
        
        Args:
            value: Quality setting to validate
            
        Returns:
            str: Validated quality
        """
        if value:
            valid_qualities = ['low', 'medium', 'high', 'lossless']
            if value.lower() not in valid_qualities:
                raise serializers.ValidationError(
                    f"Invalid quality setting. Valid options: {', '.join(valid_qualities)}"
                )
        return value.lower() if value else value
    
    def validate_sample_rate(self, value):
        """
        Validate audio sample rate.
        
        Args:
            value: Sample rate to validate
            
        Returns:
            int: Validated sample rate
        """
        if value is not None:
            valid_rates = [8000, 16000, 22050, 44100, 48000, 96000]
            if value not in valid_rates:
                raise serializers.ValidationError(
                    f"Invalid sample rate. Common rates: {', '.join(map(str, valid_rates))} Hz"
                )
        return value
    
    def validate_channels(self, value):
        """
        Validate number of audio channels.
        
        Args:
            value: Number of channels to validate
            
        Returns:
            int: Validated channel count
        """
        if value is not None:
            if value < 1 or value > 8:
                raise serializers.ValidationError(
                    "Number of channels must be between 1 and 8."
                )
        return value
    
    def validate_confidence_score(self, value):
        """
        Validate transcription confidence score.
        
        Args:
            value: Confidence score to validate
            
        Returns:
            float: Validated confidence score
        """
        if value is not None:
            if not (0.0 <= value <= 1.0):
                raise serializers.ValidationError(
                    "Confidence score must be between 0.0 and 1.0."
                )
        return value
    
    def validate_metadata(self, value):
        """
        Validate metadata JSON structure.
        
        Args:
            value: Metadata to validate
            
        Returns:
            dict: Validated metadata
        """
        if value is not None:
            if not isinstance(value, dict):
                raise serializers.ValidationError("Metadata must be a valid JSON object.")
            
            # Validate transcription job metadata if present
            if 'transcription_job_id' in value:
                if not isinstance(value['transcription_job_id'], str):
                    raise serializers.ValidationError(
                        "transcription_job_id must be a string."
                    )
        
        return value
    
    def to_representation(self, instance):
        """
        Customize representation based on user permissions and transcription status.
        
        Args:
            instance: AudioRecording instance
            
        Returns:
            dict: Customized representation
        """
        data = super().to_representation(instance)
        request = self.context.get('request')
        
        # Hide transcription data if user doesn't have permission
        if request and hasattr(request, 'user'):
            try:
                from core.models import UserProfile
                user_profile = UserProfile.objects.get(user=request.user)
                
                # Only admin and operators can see transcription data
                if user_profile.role not in ['admin', 'tenant_admin', 'operator', 'call_operator']:
                    transcription_fields = ['transcription', 'confidence_score', 'language_code']
                    for field in transcription_fields:
                        data.pop(field, None)
                        
            except UserProfile.DoesNotExist:
                pass
        
        # Remove file path for security (only provide download_url)
        data.pop('file_path', None)
        
        return data


class AudioTranscriptionSerializer(serializers.Serializer):
    """
    Serializer for audio transcription with confidence metrics.
    
    Used for transcription-specific operations and updates.
    """
    
    recording_id = serializers.IntegerField()
    transcription = serializers.CharField()
    language_code = serializers.CharField(required=False, allow_blank=True)
    confidence_score = serializers.FloatField(required=False, allow_null=True)
    
    # Transcription metadata
    job_id = serializers.CharField(required=False, allow_blank=True)
    processing_time = serializers.FloatField(required=False, allow_null=True)
    word_count = serializers.IntegerField(required=False, allow_null=True)
    
    # Timing information
    segments = serializers.JSONField(required=False, allow_null=True)
    timestamp = serializers.DateTimeField(default=timezone.now)
    
    def validate_recording_id(self, value):
        """
        Validate that the recording exists and user has access.
        
        Args:
            value: Recording ID to validate
            
        Returns:
            int: Validated recording ID
        """
        request = self.context.get('request')
        
        try:
            recording = AudioRecording.objects.get(id=value)
            
            # Check tenant access if available
            if request and hasattr(request, 'tenant'):
                if recording.call_session.tenant != request.tenant:
                    raise serializers.ValidationError(
                        "Recording does not belong to your tenant."
                    )
                    
        except AudioRecording.DoesNotExist:
            raise serializers.ValidationError("Audio recording not found.")
        
        return value
    
    def validate_confidence_score(self, value):
        """
        Validate confidence score range.
        
        Args:
            value: Confidence score to validate
            
        Returns:
            float: Validated confidence score
        """
        if value is not None:
            if not (0.0 <= value <= 1.0):
                raise serializers.ValidationError(
                    "Confidence score must be between 0.0 and 1.0."
                )
        return value
    
    def validate_language_code(self, value):
        """
        Validate language code format.
        
        Args:
            value: Language code to validate
            
        Returns:
            str: Validated language code
        """
        if value:
            # Basic validation for ISO 639-1 or 639-2 codes
            if not (2 <= len(value) <= 3 and value.isalpha()):
                raise serializers.ValidationError(
                    "Language code must be a valid ISO 639 code (2-3 letters)."
                )
        return value.lower() if value else value
    
    def validate_segments(self, value):
        """
        Validate transcription segments structure.
        
        Args:
            value: Segments data to validate
            
        Returns:
            list: Validated segments
        """
        if value is not None:
            if not isinstance(value, list):
                raise serializers.ValidationError("Segments must be a list.")
            
            for i, segment in enumerate(value):
                if not isinstance(segment, dict):
                    raise serializers.ValidationError(f"Segment {i} must be an object.")
                
                # Validate required segment fields
                required_fields = ['start_time', 'end_time', 'text']
                for field in required_fields:
                    if field not in segment:
                        raise serializers.ValidationError(
                            f"Segment {i} missing required field: {field}"
                        )
                
                # Validate timing
                try:
                    start_time = float(segment['start_time'])
                    end_time = float(segment['end_time'])
                    if start_time < 0 or end_time < 0 or start_time >= end_time:
                        raise ValueError("Invalid timing")
                except (ValueError, TypeError):
                    raise serializers.ValidationError(
                        f"Segment {i} has invalid timing values."
                    )
        
        return value
