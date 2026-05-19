from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from app.domain.interaction import ChatTurn


class MemoryType(str, Enum):
    SESSION = "session"
    CANDIDATE = "candidate"
    APPROVED = "approved"


class MemoryReviewStatus(str, Enum):
    CANDIDATE = "candidate"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class StalenessStatus(str, Enum):
    FRESH = "fresh"
    UNKNOWN = "unknown"
    STALE = "stale"


@dataclass(frozen=True)
class MemoryRef:
    memory_id: str
    memory_type: str
    review_status: str
    staleness_status: str
    score: float | None = None


@dataclass
class SessionMemory:
    """v2 §12.2."""

    session_id: str
    user_id: str | None
    project_id: str | None
    active_entities: dict[str, list[str]] = field(default_factory=dict)
    active_scenario_object: str | None = None
    active_scenario_depth: str | None = None
    conversation_summary: str = ""
    recent_turns: list[ChatTurn] = field(default_factory=list)
    last_retrieved_chunk_ids: list[str] = field(default_factory=list)
    last_memory_ids_used: list[str] = field(default_factory=list)
    updated_at: datetime | None = None
    expires_at: datetime | None = None
