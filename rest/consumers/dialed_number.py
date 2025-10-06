# Author: RA
# Purpose: Dialed Number Live Transcript
# Created: 06/10/2025
import json

from channels.generic.websocket import AsyncWebsocketConsumer

class DialedNumberLiveTranscriptConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(args, kwargs)
        self.dialed_number = None

    async def connect(self):
        self.dialed_number = self.scope['url_route']['kwargs']['dialed_number']
        await self.channel_layer.group_add(self.dialed_number, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.dialed_number, self.channel_name)


    async def receive(self, text_data=None, bytes_data=None):
        message = json.loads(text_data)["message"]


    async def send_segment(self, event):
        await self.send(text_data=json.dumps(event))
