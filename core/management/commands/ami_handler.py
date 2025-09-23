# Author: RA
# Purpose: CLI Command AMI HANDLER
# Created: 23/09/2025

import asyncio
import logging
from django.core.management.base import BaseCommand
from core.ami.events import AMIEventHandler
from core.models import Tenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Start the Asterisk AMI event listener'

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", nargs="*", type=str,
            help="Optional list of tenant IDs to register (default = all active)"
        )

    async def handle_async(self, *args, **options):
        event_handler = AMIEventHandler()
        connection = await event_handler.register_handlers(2)
        asyncio.create_task(connection.connect())
        await asyncio.get_running_loop().create_future()

    def handle(self, *args, **options):
        tenant_ids = options["tenant"]
        asyncio.run(self.handle_async(*args, **options))
