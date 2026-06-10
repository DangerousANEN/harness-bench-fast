"""WebSocket connection manager for real-time run progress."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections grouped by run_id."""

    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, run_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(run_id, []).append(ws)

    async def disconnect(self, run_id: str, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(run_id, [])
            if ws in conns:
                conns.remove(ws)
            if not conns:
                self._connections.pop(run_id, None)

    async def broadcast(self, run_id: str, data: dict[str, Any]) -> None:
        """Send a JSON message to all clients watching a run."""
        async with self._lock:
            conns = list(self._connections.get(run_id, []))

        dead: list[WebSocket] = []
        message = json.dumps(data, default=str)
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                conns = self._connections.get(run_id, [])
                for ws in dead:
                    if ws in conns:
                        conns.remove(ws)

    async def send_task_update(
        self,
        run_id: str,
        task_result: dict[str, Any],
        run_stats: dict[str, Any],
    ) -> None:
        await self.broadcast(run_id, {
            "type": "task_completed",
            "task_result": task_result,
            "run_stats": run_stats,
        })

    async def send_run_completed(self, run_id: str, run_data: dict[str, Any]) -> None:
        await self.broadcast(run_id, {
            "type": "run_completed",
            "run": run_data,
        })

    async def send_heartbeat(self, run_id: str, run_stats: dict[str, Any]) -> None:
        await self.broadcast(run_id, {
            "type": "heartbeat",
            "run_stats": run_stats,
        })

    def has_subscribers(self, run_id: str) -> bool:
        return bool(self._connections.get(run_id))


# Singleton instance
ws_manager = ConnectionManager()
