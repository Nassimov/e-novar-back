"""Queue-based WebSocket delivery manager.

Only ONE asyncio task (the sender) ever calls ws.send_json() per connection.
HTTP handlers and Redis subscribers simply put events into the asyncio.Queue.

Key design decisions:
- Each WS *connection* gets its own Queue (not one per user). This handles
  multiple tabs / reconnects without the queues clobbering each other.
- send() yields immediately after put() via asyncio.sleep(0) so the sender
  task runs before any blocking call (e.g. sync Redis publish) can hold the
  event loop and delay delivery.
- None is the sentinel that tells a sender task to stop.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Dict, Set

logger = logging.getLogger(__name__)


class ChatConnectionManager:
    """Maps user_id → set of asyncio.Queue, one queue per active WS connection."""

    def __init__(self) -> None:
        self._queues: Dict[str, Set[asyncio.Queue]] = defaultdict(set)

    def register(self, user_id: str) -> asyncio.Queue:
        """Create a fresh Queue for a new WS connection and register it."""
        q: asyncio.Queue = asyncio.Queue()
        self._queues[user_id].add(q)
        logger.debug("WS registered: user=%s total=%d", user_id, len(self._queues[user_id]))
        return q

    def unregister(self, user_id: str, q: asyncio.Queue) -> None:
        """Remove this specific connection's queue and stop its sender task."""
        s = self._queues.get(user_id)
        if s is not None:
            s.discard(q)
            if not s:
                del self._queues[user_id]
        q.put_nowait(None)  # sentinel → _sender task exits
        logger.debug("WS unregistered: user=%s", user_id)

    def is_connected(self, user_id: str) -> bool:
        return bool(self._queues.get(user_id))

    async def send(self, user_id: str, data: dict) -> bool:
        """Enqueue data to ALL active connections for user_id.

        Yields to the event loop immediately after enqueueing so that sender
        tasks run before any subsequent blocking call (e.g. sync Redis publish).
        Returns True if at least one connection was active.
        """
        queues = list(self._queues.get(user_id, set()))
        if not queues:
            return False
        for q in queues:
            q.put_nowait(data)
        # Yield so sender tasks can run NOW, before any blocking code after this call.
        await asyncio.sleep(0)
        return True

    def send_nowait(self, user_id: str, data: dict) -> bool:
        """Non-blocking enqueue (no yield). Returns True if connected."""
        queues = list(self._queues.get(user_id, set()))
        if not queues:
            return False
        for q in queues:
            q.put_nowait(data)
        return True


chat_connections = ChatConnectionManager()
