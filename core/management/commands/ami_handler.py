# Author: RA
# Purpose: CLI Command AMI HANDLER
# Created: 23/09/2025

import asyncio
import logging
import signal
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from core.asterisk_events import AsteriskEventHandlers
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
            tasks[tenant.id] = AsteriskEventHandlers(tenant_id=tenant.id)
            await tasks[tenant.id].initialize()

        try:
            logger.info("AMI & ARI event listener started.")
            await stop_event.wait()  # Wait until a shutdown signal is received
        finally:
            logger.info("Shutting down gracefully...")
            for tenant in tenants:
                await tasks[tenant.id].disconnect()
            logger.info("AMI & ARI event listener stopped.")


    def handle(self, *args, **options):
        # If you need tenant filtering, you can use it here
        tenant_ids = options["tenant"]
        try:
            asyncio.run(self.handle_async(*args, **options))
        except KeyboardInterrupt:
            logger.info("Process interrupted by user.")

