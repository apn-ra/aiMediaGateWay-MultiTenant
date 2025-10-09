# Author: RA
# Purpose: CLI Command AMI HANDLER
# Created: 23/09/2025

import asyncio
import json
import logging
import signal
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from core.asterisk_events import AsteriskEventHandlers
from channels.layers import get_channel_layer
from core.models import Tenant  # Preserved if you need tenant logic

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Start the Asterisk AMI event listener"
    group_name= "tenants_asterisk_event"
    tasks = {}

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", nargs="*", type=str,
            help="Optional list of tenant IDs to register (default = all active)",
        )

    async def process_tenant(self, message):
        if message["type"] in ["new_tenant", "enable_tenant"]:
            if message["tenant"]["id"] not in self.tasks:
                self.tasks[message['tenant']['id']] = AsteriskEventHandlers(tenant_id=message['tenant']['id'])
                await self.tasks[message['tenant']['id']].initialize()
        elif message["type"] == "disable_tenant":
            await self.tasks[message['tenant']['id']].disconnect()
            del self.tasks[message['tenant']['id']]

    async def handle_async(self, *args, **options):
        channel_layer = get_channel_layer()
        channel_name = await channel_layer.new_channel()

        await channel_layer.group_add(self.group_name, channel_name)

        tenants = await sync_to_async(list)(Tenant.objects.filter(is_active=True))

        stop_event = asyncio.Event()

        # --- Signal Handlers ---
        loop = asyncio.get_running_loop()

        def shutdown():
            logger.info("Shutdown signal received, stopping tasks...")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown)

        for tenant in tenants:
            self.tasks[tenant.id] = AsteriskEventHandlers(tenant_id=tenant.id)
            await self.tasks[tenant.id].initialize()

        try:
            # Main loop
            while not stop_event.is_set():
                try:
                    message = await asyncio.wait_for(
                        channel_layer.receive(channel_name),
                        timeout=1.0,  # check every second for stop_event
                    )
                    await self.process_tenant(message)
                except asyncio.TimeoutError:
                    # No message, loop again to check stop_event
                    continue

                except Exception as e:
                    logger.exception(f"Error while handling message: {e}")
        finally:
            tenants = await sync_to_async(list)(Tenant.objects.filter(is_active=True))
            logger.info("Shutting down gracefully...")
            for tenant in tenants:
                await self.tasks[tenant.id].disconnect()
            logger.info("AMI & ARI event listener stopped.")
            await channel_layer.group_discard(self.group_name, channel_name)

    def handle(self, *args, **options):
        # If you need tenant filtering, you can use it here
        tenant_ids = options["tenant"]
        try:
            asyncio.run(self.handle_async(*args, **options))
        except KeyboardInterrupt:
            logger.info("Process interrupted by user.")

