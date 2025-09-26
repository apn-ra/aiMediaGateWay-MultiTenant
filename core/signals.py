# Author: RA
# Purpose: django signals
# Created: 23/09/2025

# app/signals.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Tenant
from core.ami.manager import AmiClient, get_event_loop
from core.ami.registry import clients

@receiver(post_save, sender=Tenant)
def tenant_saved(sender, instance, created, **kwargs):
    loop = get_event_loop()
    if instance.is_active:
        # Start or restart
        if instance.id not in clients:
            c = AmiClient(instance)
            clients[instance.id] = c
            loop.create_task(c.run())
        else:
            # Optional: handle updates (restart connection)
            pass
    else:
        # Tenant disabled → disconnect (not shown)
        pass

@receiver(post_delete, sender=Tenant)
def tenant_deleted(sender, instance, **kwargs):
    client = clients.pop(instance.id, None)
    if client:
        loop = get_event_loop()
        loop.call_soon_threadsafe(loop.create_task, client.manager.close())
