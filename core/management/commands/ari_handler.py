# Author: RA
# Purpose: CLI Command ARI HANDLER
# Created: 22/09/2025

import asyncio
import soundfile as sf
from django.core.management.base import BaseCommand
from ari import ARIClient, ARIConfig
from core.ari.events import ARIEventHandler
from core.session.manager import get_session_manager
from core.junie_codes.rtp_integration import get_rtp_integrator
from riva.client import SpeechSynthesisService
from riva.client import Auth
from django.utils import timezone

class SimpleCallHandler:
    def __init__(self, config: ARIConfig):
        self.config = config
        self.client = None
        # self.riva_tts = SpeechSynthesisService(auth=Auth(uri="localhost:50051", use_ssl=False))

    # def create_audio_file(self, text):
    #     audio_file = sf.SoundFile("/tmp/text.wav", mode="w", samplerate=16000, channels=1)
    #     resp = self.riva_tts.synthesize(text, voice_name="English-US.Female-1", encoding="LINEAR_PCM")
    #     audio_file.write(resp.audio)
    #     audio_file.close()
    #     return audio_file.name

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

    async def test_session_rtp_integration(self):
        try:
            session_manager = get_session_manager()
            even_data = {'type': 'StasisStart',
                         'timestamp': '2025-09-23T23:42:50.723+0000',
                         'args': [], 'channel': {'id': '1758670970.74',
                                                 'name': 'PJSIP/6001-00000030',
                                                 'state': 'Ring',
                                                 'protocol_id': 'NmJkYzU4MTVjNTlhNDgwNjUzY2UwNDQwMWU4NDdlZDQ.',
                                                 'caller': {'name': '6001', 'number': '6001'},
                                                 'connected': {'name': '', 'number': ''},
                                                 'accountcode': '',
                                                 'dialplan': {'context': 'from-internal',
                                                              'exten': '2222', 'priority': 2,
                                                              'app_name': 'Stasis', 'app_data': 'live-transcript'},
                                                 'creationtime': '2025-09-23T23:42:50.719+0000', 'language': 'en'},
                         'asterisk_id': '1a:53:ae:b9:0f:79', 'application': 'live-transcript'}

            call_session = session_manager.create_session_from_event(2, even_data)
            await asyncio.sleep(2)
            sessionId = await session_manager.create_session(call_session)
            if sessionId:
                rtp_integrator = get_rtp_integrator()
                await rtp_integrator.start()

                await rtp_integrator.integrate_session(session_id=sessionId)

                # Check status
                status = await rtp_integrator.get_integration_status(sessionId)
                # print("Current Status:", status)

                # Wait for a while (simulate a call)
                await asyncio.sleep(5)
            else:
                print("Failed to create session")
                raise

        except Exception as e:
            print(f"Error in test_session_rtp_integration: {e}")
        finally:
            await rtp_integrator.de_integrate_session(session_id=sessionId)
            await rtp_integrator.stop()
            print("Cleaning up session")
            await asyncio.sleep(1)
            await session_manager.cleanup_session(sessionId)
            print("Cleaned up session Done")

    async def handle_async(self, *args, **options):
        event_handler = ARIEventHandler()
        connection = await event_handler.register_handlers(2)
        asyncio.create_task(connection.client.connect_websocket())
        await asyncio.get_running_loop().create_future()


    def handle(self, *args, **options):
        tenant_id = options['tenantId']

        # Catch Ctrl+C at the top level
        try:
            asyncio.run(self.handle_async())
        except KeyboardInterrupt:
            # User pressed Ctrl+C
            self.stdout.write(self.style.WARNING("Keyboard interrupt received. Shutting down..."))

