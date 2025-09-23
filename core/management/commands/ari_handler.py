# Author: RA
# Purpose: CLI Command ARI HANDLER
# Created: 22/09/2025

import asyncio
import soundfile as sf
from django.core.management.base import BaseCommand
from ari import ARIClient, ARIConfig
from riva.client import SpeechSynthesisService
from riva.client import Auth

class SimpleCallHandler:
    def __init__(self, config: ARIConfig):
        self.config = config
        self.client = None
        self.riva_tts = SpeechSynthesisService(auth=Auth(uri="localhost:50051", use_ssl=False))

    def create_audio_file(self, text):
        audio_file = sf.SoundFile("/tmp/text.wav", mode="w", samplerate=16000, channels=1)
        resp = self.riva_tts.synthesize(text, voice_name="English-US.Female-1", encoding="LINEAR_PCM")
        audio_file.write(resp.audio)
        audio_file.close()
        return audio_file.name

    async def start(self):
        self.client = ARIClient(self.config)
        await self.client.connect()

        # Register event handlers
        self.client.on_event('StasisStart', self.handle_stasis_start)
        self.client.on_event('ChannelCreated', self.handle_channel_created)
        self.client.on_event('ChannelDestroyed', self.handle_channel_destroyed)
        self.client.on_event('ChannelStateChange', self.handle_channel_state_changed)
        self.client.on_event('ChannelHangupRequest', self.handle_channel_hangup_request)
        self.client.on_event('StasisEnd', self.handle_statis_end)

        # Start WebSocket connection
        await self.client.connect_websocket()
        print("Call handler started")

    async def stop(self):
        await self.client.close()

    @staticmethod
    async def handle_channel_hangup_request(event_data):
        channel = event_data['channel']
        channel_id = channel['id']
        print(f"Channel Hangup Request: {channel_id}")

    @staticmethod
    async def handle_channel_state_changed(event_data):
        channel = event_data['channel']
        channel_id = channel['id']
        channel_state = channel['state']
        print(f"Channel state changed: {channel_id} -> {channel_state}")

    @staticmethod
    async def handle_channel_destroyed(event_data):
        channel = event_data['channel']
        channel_id = channel['id']
        print(f"Channel destroyed: {channel_id}")

    @staticmethod
    async def handle_channel_created(event_data):
        channel = event_data['channel']
        channel_id = channel['id']
        print(f"Channel created: {channel_id}")

    async def handle_statis_end(self, event_data):
        channel = event_data['channel']
        channel_id = channel['id']
        print(f"Channel Ended: {channel_id}")

    async def handle_stasis_start(self, event_data):
        channel = event_data['channel']
        channel_id = channel['id']

        print(f"\n\nEvent Data: {event_data}\n\n")
        # fileName = self.create_audio_file("Welcome to the Asterisk Media Gateway")
        try:
            # Answer the channel
            await self.client.answer_channel(channel_id)

            # Play welcome message
            await self.client.post(
                f"/channels/{channel_id}/play",
                json_data={'media': f'sound:demo-congrats'}
            )

            # Wait a bit then hang up
            await self.client.continue_to_dialplan(channel_id)

        except Exception as e:
            print(f"Error handling call: {e}")

class Command(BaseCommand):
    help = 'Handle ARI events'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenantId',
            type=str,
            help='Asterisk Tenant ID'
        )

    async def handle_async(self, *args, **options):
        config = ARIConfig(
            host="38.146.161.46",
            username="ari_user",
            password="Exe6RBZJOZtLTv3xKaeK",
            app_name="aiMediaGateWay_app"
        )
        handler = SimpleCallHandler(config)

        # Start ARI handler
        await handler.start()

        try:
            # Keep running until stopped
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            # Task cancelled -> exit loop
            pass
        finally:
            # Gracefully close resources
            await handler.stop()
            self.stdout.write(self.style.WARNING("Handler stopped."))

    def handle(self, *args, **options):
        tenant_id = options['tenantId']

        # Catch Ctrl+C at the top level
        try:
            asyncio.run(self.handle_async(*args, **options))
        except KeyboardInterrupt:
            # User pressed Ctrl+C
            self.stdout.write(self.style.WARNING("Keyboard interrupt received. Shutting down..."))

