# Author: RA
# Purpose: CLI Command ARI HANDLER
# Created: 22/09/2025

from django.core.management.base import BaseCommand
from ari import ARIConfig, ARIClient

class Command(BaseCommand):
    help = 'Handle ARI events'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenantId',
            type=str,
            help='Asterisk Tenant ID'
        )

    def handle(self, *args, **options):
        tenant_id = options['tenantId']
        self.stdout.write(self.style.SUCCESS(f'Handling ARI events for Tenant ID: {tenant_id}'))


