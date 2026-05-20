from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.client import Config

from app.domain.interaction import InteractionEvent
from app.ports.event_sink import EventSinkPort


class MinioEventSink(EventSinkPort):
    """S3-compatible artifact sink (MinIO local/onprem, S3 in aws-mvp).

    Boto3 with endpoint_url supports both. AWS profile uses None endpoint_url.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        endpoint_url: str | None,
        access_key: str | None,
        secret_key: str | None,
        region: str = "ap-northeast-2",
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )

    def _day(self) -> str:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _key(self, *parts: str) -> str:
        return "/".join([self._prefix, *parts]).lstrip("/")

    async def _put(self, key: str, body: bytes, *, append: bool = False) -> None:
        def _do() -> None:
            if append:
                # MinIO/S3 has no native append. Read-modify-write is fine at MVP volumes.
                try:
                    existing = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
                except self._client.exceptions.NoSuchKey:
                    existing = b""
                self._client.put_object(Bucket=self._bucket, Key=key, Body=existing + body)
            else:
                self._client.put_object(Bucket=self._bucket, Key=key, Body=body)

        await asyncio.to_thread(_do)

    async def write_interaction_event(self, event: InteractionEvent) -> None:
        key = self._key("interaction_events", self._day(), "events.jsonl")
        line = json.dumps(asdict(event), ensure_ascii=False, default=str) + "\n"
        await self._put(key, line.encode("utf-8"), append=True)

    async def write_context_snapshot(self, interaction_id: str, payload: dict[str, Any]) -> None:
        key = self._key("context_snapshots", self._day(), f"{interaction_id}.json")
        body = json.dumps(payload, ensure_ascii=False, default=str, indent=2).encode("utf-8")
        await self._put(key, body)

    async def write_prompt_render_record(
        self, interaction_id: str, payload: dict[str, Any]
    ) -> None:
        key = self._key("prompt_render_records", self._day(), f"{interaction_id}.json")
        body = json.dumps(payload, ensure_ascii=False, default=str, indent=2).encode("utf-8")
        await self._put(key, body)

    async def write_tool_result_record(
        self, interaction_id: str, payload: dict[str, Any]
    ) -> None:
        key = self._key("tool_result_records", self._day(), f"{interaction_id}.jsonl")
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        await self._put(key, line.encode("utf-8"), append=True)
