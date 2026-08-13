"""
ASGI config for games project.

HTTP is served by Django; `/ws/...` is routed to Channels consumers.
SessionMiddlewareStack gives the presence consumer access to the Django
session, which is how one browser (many tabs) is counted as one player.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'games.settings')

from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.sessions import SessionMiddlewareStack  # noqa: E402

from first.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': SessionMiddlewareStack(URLRouter(websocket_urlpatterns)),
})
