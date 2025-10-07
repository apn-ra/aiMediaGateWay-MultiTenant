"""
Custom filters for aiMediaGateway API

This module contains custom FilterSet classes that provide advanced filtering
capabilities for the API endpoints, including date range filtering, full-text
search, and complex query filtering.
"""

import django_filters
from django.db.models import Q
from django.utils import timezone
from datetime import datetime, timedelta

from core.models import CallSession, AudioRecording, UserProfile, Tenant


class CallSessionFilterSet(django_filters.FilterSet):
    """
    Advanced filtering for CallSession API endpoints
    
    Provides comprehensive filtering options including:
    - Date range filtering (start_time, end_time, created_at)
    - Duration filtering (minimum and maximum call duration)
    - Status grouping (active, completed, failed groupings)
    - User role and tenant-based filtering
    - Full-text search across multiple fields
    """
    
    # Date range filtering
    start_time_after = django_filters.DateTimeFilter(
        field_name='start_time',
        lookup_expr='gte',
        help_text='Filter sessions that started after this datetime (ISO format)'
    )
    start_time_before = django_filters.DateTimeFilter(
        field_name='start_time',
        lookup_expr='lte',
        help_text='Filter sessions that started before this datetime (ISO format)'
    )
    
    end_time_after = django_filters.DateTimeFilter(
        field_name='end_time',
        lookup_expr='gte',
        help_text='Filter sessions that ended after this datetime (ISO format)'
    )
    end_time_before = django_filters.DateTimeFilter(
        field_name='end_time',
        lookup_expr='lte',
        help_text='Filter sessions that ended before this datetime (ISO format)'
    )
    
    created_after = django_filters.DateTimeFilter(
        field_name='created_at',
        lookup_expr='gte',
        help_text='Filter sessions created after this datetime (ISO format)'
    )
    created_before = django_filters.DateTimeFilter(
        field_name='created_at',
        lookup_expr='lte',
        help_text='Filter sessions created before this datetime (ISO format)'
    )
    
    # Date shortcuts for common filtering
    today = django_filters.BooleanFilter(
        method='filter_today',
        help_text='Filter sessions from today (true/false)'
    )
    this_week = django_filters.BooleanFilter(
        method='filter_this_week',
        help_text='Filter sessions from this week (true/false)'
    )
    this_month = django_filters.BooleanFilter(
        method='filter_this_month',
        help_text='Filter sessions from this month (true/false)'
    )
    last_24h = django_filters.BooleanFilter(
        method='filter_last_24h',
        help_text='Filter sessions from last 24 hours (true/false)'
    )
    
    # Duration filtering (in seconds)
    min_duration = django_filters.NumberFilter(
        method='filter_min_duration',
        help_text='Filter sessions with minimum duration in seconds'
    )
    max_duration = django_filters.NumberFilter(
        method='filter_max_duration',
        help_text='Filter sessions with maximum duration in seconds'
    )
    
    # Status grouping filters
    is_active = django_filters.BooleanFilter(
        method='filter_active_sessions',
        help_text='Filter active sessions (active, ringing, connecting)'
    )
    is_completed = django_filters.BooleanFilter(
        method='filter_completed_sessions',
        help_text='Filter completed sessions'
    )
    is_failed = django_filters.BooleanFilter(
        method='filter_failed_sessions',
        help_text='Filter failed sessions'
    )
    
    # Multi-status filtering
    status_group = django_filters.ChoiceFilter(
        method='filter_status_group',
        choices=[
            ('active', 'Active sessions (active, ringing, connecting)'),
            ('ended', 'Ended sessions (completed, failed)'),
            ('successful', 'Successful sessions (completed)'),
            ('problematic', 'Problematic sessions (failed, timeout)'),
        ],
        help_text='Filter sessions by status group'
    )
    
    # Enhanced search
    search = django_filters.CharFilter(
        method='filter_search',
        help_text='Search across caller_id, caller_name, dialed_number, channel_name, session_id'
    )
    
    # Caller/Callee filtering
    caller_contains = django_filters.CharFilter(
        field_name='caller_id',
        lookup_expr='icontains',
        help_text='Filter by caller ID (partial match)'
    )
    caller_name_contains = django_filters.CharFilter(
        field_name='caller_name',
        lookup_expr='icontains',
        help_text='Filter by caller name (partial match)'
    )
    dialed_contains = django_filters.CharFilter(
        field_name='dialed_number',
        lookup_expr='icontains',
        help_text='Filter by dialed number (partial match)'
    )
    
    # Recording and transcription filters
    has_recordings = django_filters.BooleanFilter(
        method='filter_has_recordings',
        help_text='Filter sessions that have audio recordings'
    )
    has_transcriptions = django_filters.BooleanFilter(
        method='filter_has_transcriptions',
        help_text='Filter sessions that have transcriptions'
    )
    
    # Quality metrics filtering
    has_quality_issues = django_filters.BooleanFilter(
        method='filter_quality_issues',
        help_text='Filter sessions with quality issues (based on metadata)'
    )
    
    # User/Tenant specific filters
    user_role = django_filters.ChoiceFilter(
        method='filter_by_user_role',
        choices=[
            ('super_admin', 'Super Admin'),
            ('tenant_admin', 'Tenant Admin'),
            ('operator', 'Operator'),
            ('viewer', 'Viewer'),
        ],
        help_text='Filter based on requesting user role (admin feature)'
    )
    
    class Meta:
        model = CallSession
        fields = [
            'status',
            'direction',
            'recording_enabled',
            'transcription_enabled',
        ]
    
    def filter_today(self, queryset, name, value):
        """Filter sessions from today"""
        if value:
            today = timezone.now().date()
            return queryset.filter(start_time__date=today)
        return queryset
    
    def filter_this_week(self, queryset, name, value):
        """Filter sessions from this week"""
        if value:
            week_ago = timezone.now() - timedelta(days=7)
            return queryset.filter(start_time__gte=week_ago)
        return queryset
    
    def filter_this_month(self, queryset, name, value):
        """Filter sessions from this month"""
        if value:
            month_ago = timezone.now() - timedelta(days=30)
            return queryset.filter(start_time__gte=month_ago)
        return queryset
    
    def filter_last_24h(self, queryset, name, value):
        """Filter sessions from last 24 hours"""
        if value:
            day_ago = timezone.now() - timedelta(hours=24)
            return queryset.filter(start_time__gte=day_ago)
        return queryset
    
    def filter_min_duration(self, queryset, name, value):
        """Filter sessions with minimum duration"""
        if value is not None:
            # Calculate duration using database functions
            from django.db.models import F, Case, When, IntegerField
            from django.db.models.functions import Extract
            
            return queryset.annotate(
                duration_seconds=Case(
                    When(
                        end_time__isnull=False,
                        then=Extract('epoch', F('end_time') - F('start_time'))
                    ),
                    default=0,
                    output_field=IntegerField()
                )
            ).filter(duration_seconds__gte=value)
        return queryset
    
    def filter_max_duration(self, queryset, name, value):
        """Filter sessions with maximum duration"""
        if value is not None:
            from django.db.models import F, Case, When, IntegerField
            from django.db.models.functions import Extract
            
            return queryset.annotate(
                duration_seconds=Case(
                    When(
                        end_time__isnull=False,
                        then=Extract('epoch', F('end_time') - F('start_time'))
                    ),
                    default=0,
                    output_field=IntegerField()
                )
            ).filter(duration_seconds__lte=value)
        return queryset
    
    def filter_active_sessions(self, queryset, name, value):
        """Filter active sessions"""
        if value:
            return queryset.filter(status__in=['active', 'ringing', 'connecting'])
        elif value is False:
            return queryset.exclude(status__in=['active', 'ringing', 'connecting'])
        return queryset
    
    def filter_completed_sessions(self, queryset, name, value):
        """Filter completed sessions"""
        if value:
            return queryset.filter(status='completed')
        elif value is False:
            return queryset.exclude(status='completed')
        return queryset
    
    def filter_failed_sessions(self, queryset, name, value):
        """Filter failed sessions"""
        if value:
            return queryset.filter(status__in=['failed', 'timeout', 'error'])
        elif value is False:
            return queryset.exclude(status__in=['failed', 'timeout', 'error'])
        return queryset
    
    def filter_status_group(self, queryset, name, value):
        """Filter by status group"""
        if value == 'active':
            return queryset.filter(status__in=['active', 'ringing', 'connecting'])
        elif value == 'ended':
            return queryset.filter(status__in=['completed', 'failed', 'timeout'])
        elif value == 'successful':
            return queryset.filter(status='completed')
        elif value == 'problematic':
            return queryset.filter(status__in=['failed', 'timeout', 'error'])
        return queryset
    
    def filter_search(self, queryset, name, value):
        """Enhanced full-text search across multiple fields"""
        if value:
            return queryset.filter(
                Q(caller_id__icontains=value) |
                Q(caller_name__icontains=value) |
                Q(dialed_number__icontains=value) |
                Q(channel_name__icontains=value) |
                Q(session_id__icontains=value) |
                Q(metadata__icontains=value)  # Search in JSON metadata
            )
        return queryset
    
    def filter_has_recordings(self, queryset, name, value):
        """Filter sessions that have audio recordings"""
        if value:
            return queryset.filter(audiorecording__isnull=False).distinct()
        elif value is False:
            return queryset.filter(audiorecording__isnull=True)
        return queryset
    
    def filter_has_transcriptions(self, queryset, name, value):
        """Filter sessions that have transcriptions"""
        if value:
            return queryset.filter(
                audiorecording__transcription_text__isnull=False,
                audiorecording__transcription_text__gt=''
            ).distinct()
        elif value is False:
            return queryset.filter(
                Q(audiorecording__isnull=True) |
                Q(audiorecording__transcription_text__isnull=True) |
                Q(audiorecording__transcription_text='')
            ).distinct()
        return queryset
    
    def filter_quality_issues(self, queryset, name, value):
        """Filter sessions with quality issues based on metadata"""
        if value:
            return queryset.filter(
                Q(metadata__quality_metrics__jitter__gt=30) |
                Q(metadata__quality_metrics__packet_loss__gt=1) |
                Q(metadata__quality_metrics__mos_score__lt=3.5)
            )
        elif value is False:
            return queryset.exclude(
                Q(metadata__quality_metrics__jitter__gt=30) |
                Q(metadata__quality_metrics__packet_loss__gt=1) |
                Q(metadata__quality_metrics__mos_score__lt=3.5)
            )
        return queryset
    
    def filter_by_user_role(self, queryset, name, value):
        """Filter based on user role (admin feature)"""
        # This filter would typically be used by admins to see sessions
        # filtered by the role of users who initiated them
        if value:
            return queryset.filter(
                # Assuming we can link back to user via some relationship
                # This would need to be implemented based on actual model relationships
                tenant__userprofile__role=value
            ).distinct()
        return queryset


class AudioRecordingFilterSet(django_filters.FilterSet):
    """
    Advanced filtering for AudioRecording API endpoints
    """
    
    # Date range filtering
    created_after = django_filters.DateTimeFilter(
        field_name='created_at',
        lookup_expr='gte',
        help_text='Filter recordings created after this datetime'
    )
    created_before = django_filters.DateTimeFilter(
        field_name='created_at',
        lookup_expr='lte',
        help_text='Filter recordings created before this datetime'
    )
    
    # File size filtering
    min_file_size = django_filters.NumberFilter(
        field_name='file_size',
        lookup_expr='gte',
        help_text='Filter recordings with minimum file size in bytes'
    )
    max_file_size = django_filters.NumberFilter(
        field_name='file_size',
        lookup_expr='lte',
        help_text='Filter recordings with maximum file size in bytes'
    )
    
    # Duration filtering
    min_duration = django_filters.NumberFilter(
        field_name='duration_seconds',
        lookup_expr='gte',
        help_text='Filter recordings with minimum duration in seconds'
    )
    max_duration = django_filters.NumberFilter(
        field_name='duration_seconds',
        lookup_expr='lte',
        help_text='Filter recordings with maximum duration in seconds'
    )
    
    # Transcription filtering
    has_transcription = django_filters.BooleanFilter(
        method='filter_has_transcription',
        help_text='Filter recordings that have transcription text'
    )
    
    transcription_confidence_min = django_filters.NumberFilter(
        field_name='transcription_confidence',
        lookup_expr='gte',
        help_text='Filter recordings with minimum transcription confidence score'
    )
    
    # Call session filtering
    session_status = django_filters.CharFilter(
        field_name='call_session__status',
        help_text='Filter by call session status'
    )
    session_direction = django_filters.CharFilter(
        field_name='call_session__call_direction',
        help_text='Filter by call session direction'
    )
    
    # Search
    search = django_filters.CharFilter(
        method='filter_search',
        help_text='Search in transcription text and file names'
    )
    
    class Meta:
        model = AudioRecording
        fields = ['audio_format', 'language']
    
    def filter_has_transcription(self, queryset, name, value):
        """Filter recordings that have transcription"""
        if value:
            return queryset.filter(
                transcription_text__isnull=False,
                transcription_text__gt=''
            )
        elif value is False:
            return queryset.filter(
                Q(transcription_text__isnull=True) |
                Q(transcription_text='')
            )
        return queryset
    
    def filter_search(self, queryset, name, value):
        """Search in transcription text and file names"""
        if value:
            return queryset.filter(
                Q(transcription_text__icontains=value) |
                Q(file_path__icontains=value) |
                Q(call_session__session_id__icontains=value)
            )
        return queryset


class TenantFilterSet(django_filters.FilterSet):
    """
    Advanced filtering for Tenant API endpoints
    """
    
    # Activity filtering
    is_active = django_filters.BooleanFilter(
        field_name='is_active',
        help_text='Filter active/inactive tenants'
    )
    
    # Date filtering
    created_after = django_filters.DateTimeFilter(
        field_name='created_at',
        lookup_expr='gte',
        help_text='Filter tenants created after this datetime'
    )
    
    # Search
    search = django_filters.CharFilter(
        method='filter_search',
        help_text='Search in tenant name, slug, domain'
    )
    
    # User count filtering
    min_users = django_filters.NumberFilter(
        method='filter_min_users',
        help_text='Filter tenants with minimum number of users'
    )
    max_users = django_filters.NumberFilter(
        method='filter_max_users',
        help_text='Filter tenants with maximum number of users'
    )
    
    class Meta:
        model = Tenant
        fields = ['is_active']
    
    def filter_search(self, queryset, name, value):
        """Search in tenant fields"""
        if value:
            return queryset.filter(
                Q(name__icontains=value) |
                Q(slug__icontains=value) |
                Q(domain__icontains=value)
            )
        return queryset
    
    def filter_min_users(self, queryset, name, value):
        """Filter tenants with minimum number of users"""
        if value is not None:
            from django.db.models import Count
            return queryset.annotate(
                user_count=Count('userprofile')
            ).filter(user_count__gte=value)
        return queryset
    
    def filter_max_users(self, queryset, name, value):
        """Filter tenants with maximum number of users"""
        if value is not None:
            from django.db.models import Count
            return queryset.annotate(
                user_count=Count('userprofile')
            ).filter(user_count__lte=value)
        return queryset
