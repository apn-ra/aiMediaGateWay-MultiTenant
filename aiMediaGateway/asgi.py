"""
ASGI config for aiMediaGateway project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os
import aiMediaGateway.routing
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from core.junie_codes.websocket_middleware import WebSocketAuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aiMediaGateway.settings')

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": WebSocketAuthMiddlewareStack(
        URLRouter(
            aiMediaGateway.routing.websocket_urlpatterns
        )
    )
})
