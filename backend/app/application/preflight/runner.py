from __future__ import annotations

from collections.abc import Sequence

import structlog

from app.application.preflight.port import PreflightCheck, PreflightResult


class PreflightFailedError(RuntimeError):
    """One or more strict preflight checks reported `ok=False`.

    Raised during `build_container`; treated as a fatal boot failure (matches
    Kubernetes startup-probe semantics — traffic is never accepted with a
    broken backing service).
    """

    def __init__(self, failures: Sequence[PreflightResult]) -> None:
        msgs = ", ".join(f"{r.name}: {r.message or 'failed'}" for r in failures)
        super().__init__(f"preflight strict failures: {msgs}")
        self.failures = list(failures)


class PreflightRunner:
    """Runs all checks in order and aggregates results.

    The runner itself has no policy beyond "run, log, raise on strict failure".
    Severity is declared by each check (`PreflightCheck.severity`) at the
    adapter level — `profiles.py` picks the severity per profile when it
    builds the check list.
    """

    def __init__(self, checks: Sequence[PreflightCheck]) -> None:
        self._checks = list(checks)
        self._log = structlog.get_logger("preflight")

    async def run_all(self) -> list[PreflightResult]:
        results: list[PreflightResult] = []
        for check in self._checks:
            try:
                result = await check.run()
            except Exception as exc:  # noqa: BLE001
                result = PreflightResult(
                    name=check.name,
                    ok=False,
                    severity=check.severity,
                    message=f"unhandled error: {exc!s}"[:256],
                )
            results.append(result)
            self._log_result(result)
        strict_failures = [r for r in results if not r.ok and r.severity == "strict"]
        if strict_failures:
            raise PreflightFailedError(strict_failures)
        return results

    def _log_result(self, r: PreflightResult) -> None:
        if r.ok:
            self._log.info("preflight_ok", check=r.name, severity=r.severity)
        elif r.severity == "warn":
            self._log.warning(
                "preflight_warn", check=r.name, message=r.message, **(r.details or {})
            )
        else:
            self._log.error(
                "preflight_strict_failure",
                check=r.name,
                message=r.message,
                **(r.details or {}),
            )
