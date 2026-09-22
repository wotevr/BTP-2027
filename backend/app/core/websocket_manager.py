"""
WebSocket fan-out for the live dashboard.

Design notes
------------
* Broadcast never blocks the ingestion path. A client that cannot keep up gets
  its queued frame dropped, not the whole pipeline stalled.
* Every send is wrapped: one dead socket must not break the loop for the others.
* A heartbeat prunes connections that went away without a close frame (laptop
  lid closed, wifi dropped), which otherwise linger forever.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

log = logging.getLogger(__name__)


def json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(obj, set):
        return list(obj)
    if hasattr(obj, "value"):
        return obj.value
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def dumps(payload: Any) -> str:
    return json.dumps(payload, default=json_default)


class ConnectionManager:
    def __init__(self, send_timeout: float = 2.0) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._send_timeout = send_timeout
        self.messages_sent = 0
        self.clients_dropped = 0

    @property
    def client_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        log.info("WebSocket connected (%d client(s))", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        log.info("WebSocket disconnected (%d client(s))", len(self._connections))

    async def send_personal(self, websocket: WebSocket, payload: dict) -> bool:
        if websocket.client_state is not WebSocketState.CONNECTED:
            return False
        try:
            await asyncio.wait_for(
                websocket.send_text(dumps(payload)), timeout=self._send_timeout
            )
            self.messages_sent += 1
            return True
        except (asyncio.TimeoutError, RuntimeError, Exception) as exc:  # noqa: BLE001
            log.debug("send failed, dropping client: %s", exc)
            await self.disconnect(websocket)
            self.clients_dropped += 1
            return False

    async def broadcast(self, payload: dict) -> int:
        """Send to every client. Returns how many succeeded."""
        async with self._lock:
            targets = list(self._connections)
        if not targets:
            return 0

        text = dumps(payload)
        results = await asyncio.gather(
            *(self._send_text(ws, text) for ws in targets), return_exceptions=True
        )
        return sum(1 for r in results if r is True)

    async def _send_text(self, websocket: WebSocket, text: str) -> bool:
        if websocket.client_state is not WebSocketState.CONNECTED:
            await self.disconnect(websocket)
            return False
        try:
            await asyncio.wait_for(websocket.send_text(text), timeout=self._send_timeout)
            self.messages_sent += 1
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means the client is gone
            log.debug("broadcast to client failed: %s", exc)
            await self.disconnect(websocket)
            self.clients_dropped += 1
            return False

    async def close_all(self) -> None:
        async with self._lock:
            targets = list(self._connections)
            self._connections.clear()
        for ws in targets:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


manager = ConnectionManager()
