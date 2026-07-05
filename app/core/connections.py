"""In-process WebSocket registry for direct same-worker message delivery."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Set

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ChatConnectionManager:
    """Maps user_id → active WebSocket connections on this worker."""

    def __init__(self) -> None:
        self._connections: Dict[str, Set["WebSocket"]] = defaultdict(set)

    def register(self, user_id: str, ws: "WebSocket") -> None:
        self._connections[user_id].add(ws)

    def unregister(self, user_id: str, ws: "WebSocket") -> None:
        self._connections[user_id].discard(ws)
        if not self._connections[user_id]:
            self._connections.pop(user_id, None)

    def is_connected(self, user_id: str) -> bool:
        return bool(self._connections.get(user_id))

    async def send(self, user_id: str, data: dict) -> bool:
        """Push to all active sockets for user_id. Returns True if at least one delivered."""
        sockets = list(self._connections.get(user_id, set()))
        if not sockets:
            return False
        dead = []
        delivered = False
        for ws in sockets:
            try:
                await ws.send_json(data)
                delivered = True
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections[user_id].discard(ws)
        return delivered


chat_connections = ChatConnectionManager()
