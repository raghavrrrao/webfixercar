from django.urls import path

from .consumers import PresenceConsumer, ScoreboardConsumer

websocket_urlpatterns = [
    path('ws/presence/', PresenceConsumer.as_asgi()),
    path('ws/scoreboard/', ScoreboardConsumer.as_asgi()),
]
