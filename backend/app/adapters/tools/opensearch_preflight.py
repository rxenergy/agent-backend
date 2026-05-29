from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.application.preflight.port import PreflightCheck, PreflightResult, Severity


@dataclass
class OpenSearchPreflight:
    """ADR-0007 preflight for the OpenSearch backing service.

    Verifies (a) cluster reachability via `/_cluster/health`, (b) target
    index existence via `HEAD /<index>`, and (c) — when `required_fields` is
    non-empty — that those fields are present in the index mapping (v3.1:
    the G3 regulatory signals require `clause_id` / `authority_tier` /
    `jurisdiction` / `effective_on` to exist before the evaluator can read
    them). Severity is set by the caller — `local` profile uses `warn` so
    dev boot survives missing seed; `aws-mvp` / `onprem` use `strict` so a
    broken backing service aborts boot.
    """

    endpoint: str
    index: str
    severity: Severity
    search_pipeline: str | None = None
    required_fields: tuple[str, ...] = ()
    timeout_s: float = 5.0
    verify_certs: bool = False
    name: str = "opensearch"

    async def run(self) -> PreflightResult:
        base = self.endpoint.rstrip("/")
        details: dict[str, object] = {"endpoint": base, "index": self.index}
        if self.search_pipeline:
            details["search_pipeline"] = self.search_pipeline
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_s, verify=self.verify_certs
            ) as client:
                health = await client.get(f"{base}/_cluster/health")
                if health.status_code >= 400:
                    return PreflightResult(
                        name=self.name,
                        ok=False,
                        severity=self.severity,
                        message=f"cluster health unreachable (status={health.status_code})",
                        details=details,
                    )
                head = await client.request("HEAD", f"{base}/{self.index}")
                if head.status_code == 404:
                    return PreflightResult(
                        name=self.name,
                        ok=False,
                        severity=self.severity,
                        message="index missing (run `make seed` to populate)",
                        details=details,
                    )
                if head.status_code >= 400:
                    return PreflightResult(
                        name=self.name,
                        ok=False,
                        severity=self.severity,
                        message=f"index check failed (status={head.status_code})",
                        details=details,
                    )
                if self.search_pipeline:
                    pipe = await client.get(
                        f"{base}/_search/pipeline/{self.search_pipeline}"
                    )
                    if pipe.status_code == 404:
                        return PreflightResult(
                            name=self.name,
                            ok=False,
                            severity=self.severity,
                            message=f"search pipeline missing: {self.search_pipeline}",
                            details=details,
                        )
                    if pipe.status_code >= 400:
                        return PreflightResult(
                            name=self.name,
                            ok=False,
                            severity=self.severity,
                            message=f"search pipeline check failed (status={pipe.status_code})",
                            details=details,
                        )
                if self.required_fields:
                    mp = await client.get(f"{base}/{self.index}/_mapping")
                    if mp.status_code >= 400:
                        return PreflightResult(
                            name=self.name,
                            ok=False,
                            severity=self.severity,
                            message=f"mapping fetch failed (status={mp.status_code})",
                            details=details,
                        )
                    present = _mapping_field_names(mp.json())
                    missing = [f for f in self.required_fields if f not in present]
                    if missing:
                        details["missing_fields"] = missing
                        return PreflightResult(
                            name=self.name,
                            ok=False,
                            severity=self.severity,
                            message=(
                                "index mapping missing required fields "
                                f"{missing} (re-create index with the v3.1 "
                                "regulatory-meta mapping)"
                            ),
                            details=details,
                        )
        except httpx.RequestError as exc:
            return PreflightResult(
                name=self.name,
                ok=False,
                severity=self.severity,
                message=f"unreachable: {exc!s}"[:200],
                details=details,
            )
        return PreflightResult(
            name=self.name, ok=True, severity=self.severity, details=details
        )


def _mapping_field_names(mapping_response: dict) -> set[str]:
    """Top-level field names across all indices in a `GET /<index>/_mapping`
    response. Shape: `{ "<index>": { "mappings": { "properties": {...} } } }`
    — the index key may be a concrete name behind an alias, so union over all
    entries. Only top-level properties are checked (the v3.1 regulatory-meta
    fields live at the document root, not under `doc_metadata`)."""
    names: set[str] = set()
    for entry in (mapping_response or {}).values():
        props = ((entry or {}).get("mappings") or {}).get("properties") or {}
        names.update(props.keys())
    return names


_ = PreflightCheck  # type: ignore[unused-ignore]  # static protocol satisfaction
