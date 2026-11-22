"""WebSocket connection registry + broker (in-process, or Redis pub/sub when REDIS_URL is set)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

from app.core.config import get_settings

log = logging.getLogger(__name__)
CHANNEL = "cafe:events"


class ConnectionManager:
    def __init__(self) -> None:
        self.topics: dict[str, set[WebSocket]] = defaultdict(set)
        self.by_socket: dict[WebSocket, set[str]] = defaultdict(set)

    def subscribe(self, ws: WebSocket, topic: str) -> None:
        self.topics[topic].add(ws)
        self.by_socket[ws].add(topic)

    def unsubscribe(self, ws: WebSocket, topic: str) -> None:
        self.topics[topic].discard(ws)
        self.by_socket[ws].discard(topic)

    def is_subscribed(self, topic: str, role: str, user_id: str) -> bool:
        for ws in self.topics.get(topic, ()):
            if getattr(ws.state, "principal_key", None) == (role, user_id):
                return True
        return False

    def disconnect(self, ws: WebSocket) -> None:
        for t in self.by_socket.pop(ws, set()):
            self.topics[t].discard(ws)
            if not self.topics[t]:
                self.topics.pop(t, None)

    async def deliver_local(self, topic: str, envelope: dict) -> None:
        dead = []
        for ws in list(self.topics.get(topic, ())):
            try:
                await ws.send_json(envelope)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


class Broker:
    def __init__(self) -> None:
        self._redis = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        url = get_settings().redis_url
        if not url:
            return
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=True)
        self._task = asyncio.create_task(self._listen(), name="redis-broker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._redis:
            await self._redis.aclose()
        self._redis, self._task = None, None

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    data = json.loads(msg["data"])
                    await manager.deliver_local(data["topic"], data["envelope"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("redis broker listener crashed; retrying")
                await asyncio.sleep(2)

    async def publish(self, topic: str, envelope: dict) -> None:
        if self._redis is not None:
            try:
                await self._redis.publish(CHANNEL, json.dumps({"topic": topic, "envelope": envelope}, default=str))
                return
            except Exception:
                log.exception("redis publish failed, falling back to local delivery")
        await manager.deliver_local(topic, envelope)


broker = Broker()
