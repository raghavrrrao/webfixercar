"""
Who is online right now.

Presence is keyed by Django *session*, not by socket: one browser with five
tabs open is one player. Entries carry a last-seen timestamp that the client
refreshes with a heartbeat, so sockets dropped without a close frame (laptop
lid, instance restart, dead wifi) age out instead of inflating the count.

Two interchangeable stores:

* InMemoryPresence -- the default. Accurate for a single process, which is
  what `runserver` and a single Render instance are.
* RedisPresence    -- used automatically when REDIS_URL is set, so several
  instances share one count.
"""

import time

from django.conf import settings

PRESENCE_GROUP = 'wf_presence'

# Client pings on this interval; entries older than the timeout are dropped.
HEARTBEAT_INTERVAL_SECONDS = 20
PRESENCE_TIMEOUT_SECONDS = 55

_REDIS_KEY = 'wf:presence:connections'


class InMemoryPresence:
    def __init__(self):
        # connection id -> (presence key, last seen timestamp)
        self._connections = {}

    def _sweep(self):
        cutoff = time.time() - PRESENCE_TIMEOUT_SECONDS
        stale = [cid for cid, (_, seen) in self._connections.items() if seen < cutoff]
        for cid in stale:
            del self._connections[cid]

    async def touch(self, connection_id, presence_key):
        self._connections[connection_id] = (presence_key, time.time())

    async def remove(self, connection_id):
        self._connections.pop(connection_id, None)

    async def count(self):
        self._sweep()
        return len({key for key, _ in self._connections.values()})


class RedisPresence:
    """Sorted set of "<presence key>|<connection id>" scored by last-seen."""

    def __init__(self, url):
        import redis.asyncio as redis  # provided by channels-redis

        self._redis = redis.from_url(url, decode_responses=True)

    @staticmethod
    def _member(connection_id, presence_key):
        return f'{presence_key}|{connection_id}'

    async def touch(self, connection_id, presence_key):
        await self._redis.zadd(_REDIS_KEY, {self._member(connection_id, presence_key): time.time()})

    async def remove(self, connection_id):
        members = await self._redis.zrange(_REDIS_KEY, 0, -1)
        doomed = [m for m in members if m.rsplit('|', 1)[-1] == connection_id]
        if doomed:
            await self._redis.zrem(_REDIS_KEY, *doomed)

    async def count(self):
        cutoff = time.time() - PRESENCE_TIMEOUT_SECONDS
        await self._redis.zremrangebyscore(_REDIS_KEY, '-inf', cutoff)
        members = await self._redis.zrange(_REDIS_KEY, 0, -1)
        return len({m.rsplit('|', 1)[0] for m in members})


_store = None


def get_presence_store():
    global _store
    if _store is None:
        url = getattr(settings, 'REDIS_URL', '')
        _store = RedisPresence(url) if url else InMemoryPresence()
    return _store
