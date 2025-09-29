# Author: RA
# Purpose: CLI Command AMI HANDLER
# Created: 23/09/2025

import asyncio
import logging
import signal
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from core.ami.events import AMIEventHandler
from core.models import Tenant  # Preserved if you need tenant logic

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Start the Asterisk AMI event listener"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", nargs="*", type=str,
            help="Optional list of tenant IDs to register (default = all active)",
        )

    async def handle_async(self, *args, **options):
        event_handler = AMIEventHandler()
        tenants = await sync_to_async(list)(Tenant.objects.filter(is_active=True))
        tasks = {}

        stop_event = asyncio.Event()

        # --- Signal Handlers ---
        loop = asyncio.get_running_loop()

        def shutdown():
            logger.info("Shutdown signal received, stopping tasks...")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown)

        for tenant in tenants:
            ami_connection = await event_handler.register_ami_handlers(tenant_id=tenant.id)
            ari_connection = await event_handler.register_ari_handler(tenant_id=tenant.id)

            # --- Start AMI connection ---
            ami_connection_task = asyncio.create_task(ami_connection.connect(), name="ami-connection")
            ari_connection_task = asyncio.create_task(ari_connection.client.connect_websocket(), name="ari-connection")

            tasks[tenant.id] = {
                "ami_connection": ami_connection_task,
                "ari_connection": ari_connection_task,
            }

        try:
            logger.info("AMI event listener started.")
            await stop_event.wait()  # Wait until a shutdown signal is received
        finally:
            logger.info("Shutting down gracefully...")

            for tenant in tenants:
                tasks[tenant.id]["ami_connection"].cancel()
                tasks[tenant.id]["ari_connection"].cancel()
                try:
                    await tasks[tenant.id]["ami_connection"]
                    await tasks[tenant.id]["ari_connection"]
                except asyncio.CancelledError:
                    logger.info("AMI & ARI connection task cancelled.")

            await event_handler.ami_manager.disconnect_all()
            logger.info("AMI event listener stopped.")
            await event_handler.rtp_integrator.stop()
            logger.info("RTP Integrator stopped.")
            await event_handler.ari_event_handler.ari_manager.disconnect_all()
            logger.info("ARI event listener stopped.")


    def handle(self, *args, **options):
        # If you need tenant filtering, you can use it here
        tenant_ids = options["tenant"]
        try:
            asyncio.run(self.handle_async(*args, **options))
        except KeyboardInterrupt:
            logger.info("Process interrupted by user.")

