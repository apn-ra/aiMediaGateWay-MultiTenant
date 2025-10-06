"""
CallSession serializers for aiMediaGateway

This module contains serializers for the CallSession model with nested relationships
and specialized serializers for different use cases.
"""

from rest_framework import serializers
from django.utils import timezone
from core.models import CallSession, Tenant, UserProfile


class CallSessionSerializer(serializers.ModelSerializer):
    """
    Standard serializer for CallSession model with nested relationships.
    
    Provides basic call session information with related model data.
    """
    
    # Read-only fields
    id = serializers.IntegerField(read_only=True)
    session_id = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    
    # Tenant information
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_slug = serializers.CharField(source='tenant.slug', read_only=True)
    
    # Computed fields
    duration_seconds = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()
    recording_count = serializers.SerializerMethodField()
    
    class Meta:
        model = CallSession
        fields = [
            'id',
            'session_id',
            'caller_id',
            'caller_name',
            'dialed_number',
            'call_direction',
            'channel_name',
            'uniqueid',
            'status',
            'start_time',
            'end_time',
            'duration_seconds',
            'is_active',
            'tenant',
            'tenant_name',
            'tenant_slug',
            'recording_enabled',
            'transcription_enabled',
            'recording_count',
            'metadata',
            'created_at',
            'updated_at',
        ]
        
        extra_kwargs = {
            'tenant': {'read_only': True},  # Set by middleware/context
            'session_id': {'read_only': True},  # Auto-generated
        }
    
    def get_duration_seconds(self, obj):
        """
        Calculate call duration in seconds.
        
        Args:
            obj: CallSession instance
            
        Returns:
            int: Duration in seconds or None if call not ended
        """
        if obj.end_time and obj.start_time:
            return int((obj.end_time - obj.start_time).total_seconds())
        elif obj.start_time and obj.status in ['active', 'ringing']:
            return int((timezone.now() - obj.start_time).total_seconds())
        return None
    
    def get_is_active(self, obj):
        """
        Check if the call session is currently active.
        
        Args:
            obj: CallSession instance
            
        Returns:
            bool: True if call is active
        """
        return obj.status in ['active', 'ringing', 'connecting']
    
    def get_recording_count(self, obj):
        """
        Get the number of audio recordings for this session.
        
        Args:
            obj: CallSession instance
            
        Returns:
            int: Number of recordings
        """
        return obj.audiorecording_set.count()
    
    def validate_caller_id(self, value):
        """
        Validate caller ID format.
        
        Args:
            value: Caller ID to validate
            
        Returns:
            str: Validated caller ID
        """
        if value:
            # Remove non-digit characters for validation
            digits_only = ''.join(filter(str.isdigit, value))
            if not digits_only or len(digits_only) < 7:
                raise serializers.ValidationError(
                    "Caller ID must contain at least 7 digits."
                )
        return value
    
    def validate_dialed_number(self, value):
        """
        Validate dialed number format.
        
        Args:
            value: Dialed number to validate
            
        Returns:
            str: Validated dialed number
        """
        if value:
            # Remove non-digit characters for validation
            digits_only = ''.join(filter(str.isdigit, value))
            if not digits_only or len(digits_only) < 3:
                raise serializers.ValidationError(
                    "Dialed number must contain at least 3 digits."
                )
        return value
    
    def validate_metadata(self, value):
        """
        Validate metadata JSON structure.
        
        Args:
            value: Metadata dictionary to validate
            
        Returns:
            dict: Validated metadata
        """
        if value is not None and not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be a valid JSON object.")
        return value


class CallSessionDetailSerializer(CallSessionSerializer):
    """
    Detailed serializer for CallSession with additional nested data.
    
    Includes audio recordings, transcriptions, and extended metadata.
    """
    
    # Nested relationships
    recordings = serializers.SerializerMethodField()
    transcriptions = serializers.SerializerMethodField()
    
    # Additional computed fields
    call_quality_metrics = serializers.SerializerMethodField()
    participant_count = serializers.SerializerMethodField()
    
    class Meta(CallSessionSerializer.Meta):
        fields = CallSessionSerializer.Meta.fields + [
            'recordings',
            'transcriptions',
            'call_quality_metrics',
            'participant_count',
        ]
    
    def get_recordings(self, obj):
        """
        Get audio recordings for this session.
        
        Args:
            obj: CallSession instance
            
        Returns:
            list: List of recording data
        """
        recordings = obj.audiorecording_set.all()
        return [
            {
                'id': recording.id,
                'filename': recording.filename,
                'file_path': recording.file_path,
                'file_size': recording.file_size,
                'duration': recording.duration,
                'format': recording.format,
                'quality': recording.quality,
                'created_at': recording.created_at,
            }
            for recording in recordings
        ]
    
    def get_transcriptions(self, obj):
        """
        Get transcriptions for this session's recordings.
        
        Args:
            obj: CallSession instance
            
        Returns:
            list: List of transcription data
        """
        transcriptions = []
        for recording in obj.audiorecording_set.all():
            if hasattr(recording, 'transcription') and recording.transcription:
                transcriptions.append({
                    'recording_id': recording.id,
                    'text': recording.transcription,
                    'confidence': getattr(recording, 'confidence_score', None),
                    'language': getattr(recording, 'detected_language', None),
                })
        return transcriptions
    
    def get_call_quality_metrics(self, obj):
        """
        Get call quality metrics from metadata.
        
        Args:
            obj: CallSession instance
            
        Returns:
            dict: Call quality metrics
        """
        if obj.metadata and 'quality_metrics' in obj.metadata:
            return obj.metadata['quality_metrics']
        
        # Return default metrics structure
        return {
            'jitter': None,
            'packet_loss': None,
            'latency': None,
            'mos_score': None,
        }
    
    def get_participant_count(self, obj):
        """
        Get number of participants from metadata.
        
        Args:
            obj: CallSession instance
            
        Returns:
            int: Number of participants
        """
        if obj.metadata and 'participants' in obj.metadata:
            return len(obj.metadata['participants'])
        return 2  # Default: caller and called party


class CallSessionCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating new CallSession instances.
    
    Optimized for call session creation with required fields only.
    """
    
    class Meta:
        model = CallSession
        fields = [
            'caller_id',
            'caller_name',
            'dialed_number',
            'call_direction',
            'channel_name',
            'uniqueid',
            'recording_enabled',
            'transcription_enabled',
            'metadata',
        ]
    
    def validate(self, data):
        """
        Validate the complete data set for session creation.
        
        Args:
            data: Complete validated data
            
        Returns:
            dict: Validated data
        """
        # Ensure required fields are present
        required_fields = ['caller_id', 'dialed_number', 'call_direction', 'uniqueid']
        for field in required_fields:
            if not data.get(field):
                raise serializers.ValidationError(f"{field} is required for session creation.")
        
        # Validate call direction
        if data['call_direction'] not in ['inbound', 'outbound']:
            raise serializers.ValidationError(
                "call_direction must be either 'inbound' or 'outbound'."
            )
        
        return data
    
    def create(self, validated_data):
        """
        Create a new CallSession with auto-generated session_id.
        
        Args:
            validated_data: Validated data from serializer
            
        Returns:
            CallSession: Created session instance
        """
        # Set tenant from request context
        request = self.context.get('request')
        if request and hasattr(request, 'tenant'):
            validated_data['tenant'] = request.tenant
        
        # Set status to 'connecting' for new sessions
        validated_data['status'] = 'connecting'
        
        # Set start_time to now
        validated_data['start_time'] = timezone.now()
        
        return super().create(validated_data)


class LiveCallStatusSerializer(serializers.Serializer):
    """
    Serializer for real-time call status updates.
    
    Used for WebSocket communication and live dashboard updates.
    """
    
    session_id = serializers.CharField()
    status = serializers.ChoiceField(choices=CallSession.STATUS_CHOICES)
    caller_id = serializers.CharField()
    dialed_number = serializers.CharField()
    duration_seconds = serializers.IntegerField(required=False, allow_null=True)
    recording_status = serializers.CharField(required=False)
    participant_count = serializers.IntegerField(default=2)
    quality_metrics = serializers.JSONField(required=False, allow_null=True)
    timestamp = serializers.DateTimeField(default=timezone.now)
    
    # Tenant context
    tenant_id = serializers.IntegerField(required=False)
    tenant_slug = serializers.CharField(required=False)
    
    def validate_quality_metrics(self, value):
        """
        Validate quality metrics structure.
        
        Args:
            value: Quality metrics to validate
            
        Returns:
            dict: Validated quality metrics
        """
        if value is not None:
            if not isinstance(value, dict):
                raise serializers.ValidationError("Quality metrics must be a JSON object.")
            
            # Validate expected fields
            expected_fields = ['jitter', 'packet_loss', 'latency', 'mos_score']
            for field in expected_fields:
                if field in value and value[field] is not None:
                    if not isinstance(value[field], (int, float)):
                        raise serializers.ValidationError(
                            f"Quality metric '{field}' must be a number."
                        )
        
        return value
    
    def validate_recording_status(self, value):
        """
        Validate recording status.
        
        Args:
            value: Recording status to validate
            
        Returns:
            str: Validated recording status
        """
        if value:
            valid_statuses = ['active', 'paused', 'stopped', 'failed']
            if value not in valid_statuses:
                raise serializers.ValidationError(
                    f"Recording status must be one of: {', '.join(valid_statuses)}"
                )
        return value
