from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.interaction import InteractionEvent
from app.ports.event_sink import EventSinkPort


class FilesystemEventSink(EventSinkPort):
    def __init__(self, root: str, prefix: str) -> None:
        self._root = Path(root) / prefix
        self._lock = asyncio.Lock()

    def _day(self) -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    async def write_interaction_event(self, event: InteractionEvent) -> None:
        day = self._day()
        path = self._root / "interaction_events" / day / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(event), ensure_ascii=False, default=str)
        async with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    async def write_context_snapshot(self, interaction_id: str, payload: dict[str, Any]) -> None:
        day = self._day()
        path = self._root / "context_snapshots" / day / f"{interaction_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2))

    async def write_prompt_render_record(
        self, interaction_id: str, payload: dict[str, Any]
    ) -> None:
        day = self._day()
        path = self._root / "prompt_render_records" / day / f"{interaction_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2))

    async def write_tool_result_record(
        self, interaction_id: str, payload: dict[str, Any]
    ) -> None:
        day = self._day()
        path = self._root / "tool_result_records" / day / f"{interaction_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        async with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
