# Author: RA
# Purpose: WebSocket Routing
# Created: 06/10/2025
# Updated: 06/10/2025 - Added comprehensive WebSocket routing for all consumers

from django.urls import re_path
from rest.consumers.dialed_number import DialedNumberLiveTranscriptConsumer
from core.junie_codes.websocket_consumers import (
    CallMonitoringConsumer,
    LiveAudioStreamConsumer,
    SystemStatusConsumer,
    AdminNotificationConsumer,
)

websocket_urlpatterns = [
    # Call transcript consumer (existing)
    re_path(r'ws/call_transcript/(?P<phone_number>\d+)/$', DialedNumberLiveTranscriptConsumer.as_asgi()),
    
    # Call monitoring consumers
    re_path(r'ws/calls/monitor/$', CallMonitoringConsumer.as_asgi()),
    re_path(r'ws/calls/(?P<session_id>\w+)/monitor/$', CallMonitoringConsumer.as_asgi()),
    
    # Live audio streaming consumers with quality parameters
    re_path(r'ws/audio/stream/(?P<session_id>\w+)/$', LiveAudioStreamConsumer.as_asgi()),
    re_path(r'ws/audio/stream/(?P<session_id>\w+)/(?P<quality>\w+)/$', LiveAudioStreamConsumer.as_asgi()),
    
    # System status consumers
    re_path(r'ws/system/status/$', SystemStatusConsumer.as_asgi()),
    re_path(r'ws/system/health/$', SystemStatusConsumer.as_asgi()),
    
    # Admin notification consumers
    re_path(r'ws/admin/notifications/$', AdminNotificationConsumer.as_asgi()),
    re_path(r'ws/admin/alerts/$', AdminNotificationConsumer.as_asgi()),
]
