from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Severity = Literal["warn", "strict"]


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of a single backing-service check.

    `ok=False` with `severity="strict"` causes `PreflightRunner` to abort
    container boot (12-Factor §IV: backing services must be verified before
    serving traffic; K8s startup probe semantics).
    """

    name: str
    ok: bool
    severity: Severity
    message: str = ""
    details: dict[str, str] | None = None


class PreflightCheck(Protocol):
    """One reachability/readiness check against a backing service.

    Implementations live next to the adapter they verify (e.g. OpenSearch
    retriever, Postgres pool, MinIO sink, Phoenix prompt source). Each adapter
    owns its own preflight — `profiles.py` only assembles the list.
    """

    name: str
    severity: Severity

    async def run(self) -> PreflightResult: ...
