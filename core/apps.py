from django.apps import AppConfig
from core.ami.loop_holder import start_loop, loop

class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    # def ready(self):
    #     # Don't query DB here!
    #     # Schedule a task to initialize tenants after startup
    #     from django.db import connections
    #     from threading import Timer
    #     Timer(1, self.init_tenants).start()  # delay 1s
    #
    # def init_tenants(self):
    #     from core.models import Tenant
    #     from core.ami.client import AmiClient
    #     from core.ami.ami_registry import clients
    #
    #     for t in Tenant.objects.filter(is_active=True):
    #         c = AmiClient(t)
    #         clients[t.id] = c
    #         c.start_background()

