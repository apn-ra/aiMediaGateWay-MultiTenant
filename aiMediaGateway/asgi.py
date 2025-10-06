"""
ASGI config for aiMediaGateway project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os
import aiMediaGateway.routing
from channels.security.websocket import OriginValidator
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aiMediaGateway.settings')

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": OriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                aiMediaGateway.routing.websocket_urlpatterns
            )
        ),
        ['pbx01.apntelecom.com', 'pbx01.apntelecom.com:8080'],
    )
})
