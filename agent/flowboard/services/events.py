"""Per-shot event bus for live updates streamed to the frontend."""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any


class ShotBus:
    def __init__(self) -> None:
        self._queues: dict[uuid.UUID, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, shot_id: uuid.UUID) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[shot_id].append(q)
        return q

    def unsubscribe(self, shot_id: uuid.UUID, q: asyncio.Queue) -> None:
        if q in self._queues.get(shot_id, []):
            self._queues[shot_id].remove(q)

    async def publish(self, shot_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        for q in list(self._queues.get(shot_id, [])):
            await q.put(payload)


shot_bus = ShotBus()
