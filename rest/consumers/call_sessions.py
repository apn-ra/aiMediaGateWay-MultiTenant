# Author: RA
# Purpose: Call Monitoring Consumer
# Created: 08/10/2025
import json

from asgiref.sync import sync_to_async
from core.models import CallSession
from channels.generic.websocket import AsyncWebsocketConsumer


class CallSessionsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add("active_sessions", self.channel_name)
        await self.accept()
        await self.send(text_data=json.dumps(await self.get_sessions()))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("active_sessions", self.channel_name)

    @staticmethod
    async def get_sessions():
        return await sync_to_async(list)(CallSession.objects.get_active_sessions())

    async def new_session(self, event):
        await self.send(text_data=json.dumps(event))

    async def update_session(self, event):
        await self.send(text_data=json.dumps(event))


class CallSessionConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(args, kwargs)
        self.session_id = None

    async def connect(self):
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        await self.channel_layer.group_add(self.session_id, self.channel_name)
        await self.accept()
