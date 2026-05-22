from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.application.preflight.port import PreflightCheck, PreflightResult, Severity


@dataclass
class OpenSearchPreflight:
    """ADR-0007 preflight for the OpenSearch backing service.

    Verifies (a) cluster reachability via `/_cluster/health` and (b) target
    index existence via `HEAD /<index>`. Severity is set by the caller —
    `local` profile uses `warn` so dev boot survives missing seed; `aws-mvp`
    / `onprem` use `strict` so a broken backing service aborts boot.
    """

    endpoint: str
    index: str
    severity: Severity
    search_pipeline: str | None = None
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


_ = PreflightCheck  # type: ignore[unused-ignore]  # static protocol satisfaction
