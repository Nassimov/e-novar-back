"""Queue-based WebSocket delivery manager.

Only ONE asyncio task (the sender) ever calls ws.send_json() per connection.
HTTP handlers and Redis subscribers simply put events into the asyncio.Queue.
This avoids concurrent-write races on the WebSocket transport.

None is the sentinel that tells the sender task to stop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class ChatConnectionManager:
    """Maps user_id → asyncio.Queue[dict | None].

    Usage:
        q = chat_connections.register(user_id)
        # start sender task: reads from q, calls ws.send_json()
        ...
        chat_connections.unregister(user_id)  # puts None sentinel, removes mapping
    """

    def __init__(self) -> None:
        self._queues: Dict[str, asyncio.Queue] = {}

    def register(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[user_id] = q
        return q

    def unregister(self, user_id: str) -> None:
        q = self._queues.pop(user_id, None)
        if q is not None:
            q.put_nowait(None)  # sentinel → sender task exits cleanly

    def is_connected(self, user_id: str) -> bool:
        return user_id in self._queues

    async def send(self, user_id: str, data: dict) -> bool:
        """Enqueue data for delivery. Returns True if user is currently connected."""
        q = self._queues.get(user_id)
        if q is None:
            return False
        await q.put(data)
        return True

    def send_nowait(self, user_id: str, data: dict) -> bool:
        """Non-blocking enqueue. Returns True if user is currently connected."""
        q = self._queues.get(user_id)
        if q is None:
            return False
        q.put_nowait(data)
        return True


chat_connections = ChatConnectionManager()
