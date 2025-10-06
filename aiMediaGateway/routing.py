# Author: RA
# Purpose: WebSocket Routing
# Created: 06/10/2025

from django.urls import re_path
from rest.consumers.dialed_number import DialedNumberLiveTranscriptConsumer

websocket_urlpatterns = [
    re_path('ws/call_transcript/(?P<phone_number>\d+)/$', DialedNumberLiveTranscriptConsumer.as_asgi())
]
