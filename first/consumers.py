"""WebSocket consumer backing the live "players online" counter."""

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .presence import (
    HEARTBEAT_INTERVAL_SECONDS,
    PRESENCE_GROUP,
    get_presence_store,
)


class PresenceConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.store = get_presence_store()
        self.connection_id = self.channel_name
        self.presence_key = self._presence_key()

        await self.channel_layer.group_add(PRESENCE_GROUP, self.channel_name)
        await self.accept()
        await self.send_json({
            'type': 'welcome',
            'heartbeat': HEARTBEAT_INTERVAL_SECONDS,
        })
        await self.store.touch(self.connection_id, self.presence_key)
        await self._broadcast()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(PRESENCE_GROUP, self.channel_name)
        if hasattr(self, 'store'):
            await self.store.remove(self.connection_id)
            await self._broadcast()

    async def receive_json(self, content, **kwargs):
        # Keepalive. Also the moment we re-check for stale connections.
        if content.get('type') == 'ping':
            await self.store.touch(self.connection_id, self.presence_key)
            await self.send_json({'type': 'pong', 'count': await self.store.count()})

    async def presence_count(self, event):
        await self.send_json({'type': 'count', 'count': event['count']})

    # -- internals ---------------------------------------------------------

    def _presence_key(self):
        """One browser session = one player, however many tabs it opens."""
        session = self.scope.get('session')
        key = getattr(session, 'session_key', None) if session else None
        return f'session:{key}' if key else f'socket:{self.channel_name}'

    async def _broadcast(self):
        await self.channel_layer.group_send(
            PRESENCE_GROUP,
            {'type': 'presence.count', 'count': await self.store.count()},
        )
