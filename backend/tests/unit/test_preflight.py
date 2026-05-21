from __future__ import annotations

import pytest

from app.application.preflight.port import PreflightCheck, PreflightResult
from app.application.preflight.runner import PreflightFailedError, PreflightRunner


class _Stub:
    """Minimal PreflightCheck stub for testing PreflightRunner policy."""

    def __init__(self, name: str, ok: bool, severity: str, msg: str = "") -> None:
        self.name = name
        self.severity = severity  # type: ignore[assignment]
        self._ok = ok
        self._msg = msg

    async def run(self) -> PreflightResult:
        return PreflightResult(
            name=self.name, ok=self._ok, severity=self.severity, message=self._msg
        )


@pytest.mark.asyncio
async def test_strict_failure_aborts_boot() -> None:
    runner = PreflightRunner([_Stub("opensearch", ok=False, severity="strict", msg="x")])
    with pytest.raises(PreflightFailedError) as excinfo:
        await runner.run_all()
    assert "opensearch" in str(excinfo.value)


@pytest.mark.asyncio
async def test_warn_failure_does_not_abort() -> None:
    runner = PreflightRunner([_Stub("opensearch", ok=False, severity="warn")])
    results = await runner.run_all()
    assert len(results) == 1 and not results[0].ok


@pytest.mark.asyncio
async def test_unhandled_exception_becomes_failure() -> None:
    class _Boom:
        name = "boom"
        severity = "strict"  # type: ignore[assignment]

        async def run(self) -> PreflightResult:
            raise RuntimeError("backend dead")

    with pytest.raises(PreflightFailedError):
        await PreflightRunner([_Boom()]).run_all()


@pytest.mark.asyncio
async def test_runs_all_checks_before_raising() -> None:
    s1 = _Stub("a", ok=True, severity="strict")
    s2 = _Stub("b", ok=False, severity="strict", msg="b is down")
    s3 = _Stub("c", ok=True, severity="warn")
    runner = PreflightRunner([s1, s2, s3])
    with pytest.raises(PreflightFailedError) as excinfo:
        await runner.run_all()
    # Even though s2 fails, s3 still runs (no short-circuit).
    failures = excinfo.value.failures
    assert {f.name for f in failures} == {"b"}


def test_preflight_check_is_protocol_compliant() -> None:
    """Static check that _Stub satisfies PreflightCheck (runtime protocol)."""
    stub: PreflightCheck = _Stub("x", ok=True, severity="warn")
    assert stub.name == "x"
