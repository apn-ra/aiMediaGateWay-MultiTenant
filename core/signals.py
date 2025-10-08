# Author: RA
# Purpose: django signals
# Created: 23/09/2025

import logging

from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from core.models import UserProfile, CallSession
from asgiref.sync import async_to_sync
from django.forms.models import model_to_dict

logger = logging.getLogger(__name__)

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    instance.profile.save()


@receiver(post_save, sender=CallSession)
def create_call_session(sender, instance, created, **kwargs):
    if created:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "active_sessions", {
                "type": "new_session",
                "session": model_to_dict(instance)
            }
        )

@receiver(post_save, sender=CallSession)
def update_call_session(sender, instance, **kwargs):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "active_sessions", {
            "type": "update_session",
            "session": model_to_dict(instance)
        }
    )
