# Author: RA
# Purpose: CLI Command AMI HANDLER
# Created: 23/09/2025

import asyncio
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
    group_name= "tenants_asterisk_events"
    tasks = {}
    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", nargs="*", type=str,
            help="Optional list of tenant IDs to register (default = all active)",
        )

    async def new_tenant(self, message):
        logger.info(f"New tenant: {message}")
        # self.tasks[tenant_id] = AsteriskEventHandlers(tenant_id=tenant_id)
        # await self.tasks[tenant_id].initialize()

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
            message = await channel_layer.receive(channel_name)
            await self.new_tenant(message)
            logger.info("AMI & ARI event listener started.")
            await stop_event.wait()  # Wait until a shutdown signal is received
        finally:
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

