"""openinference._truncate — span 속성 값 크기 상한(C1).

거대 슬롯 프롬프트(full chunk body)가 span 속성으로 무제한 실려 트레이스가 수 MB 까지
부푸는 것을 앱 단에서 막는다. 통상 프롬프트는 무손실, 병리적 본문만 마커와 함께 절단.
"""

from __future__ import annotations

import importlib

from app.observability import openinference as oi


def test_small_value_is_untouched() -> None:
    s = "짧은 프롬프트 본문"
    assert oi._truncate(s, limit=65536) == s


def test_oversized_value_is_truncated_with_marker() -> None:
    s = "x" * 100_000
    out = oi._truncate(s, limit=1000)
    assert out.startswith("x" * 1000)
    assert out.endswith("context_snapshot]")  # 절단 마커(조용한 절단 금지).
    assert len(out) < len(s)


def test_zero_or_negative_limit_means_unlimited() -> None:
    s = "y" * 50_000
    assert oi._truncate(s, limit=0) == s
    assert oi._truncate(s, limit=-1) == s


def test_default_cap_from_env(monkeypatch) -> None:
    # 빈/미설정 → 64KB 기본. 명시값은 그대로. 잘못된 값은 기본으로 graceful.
    monkeypatch.delenv("OTEL_SPAN_ATTR_MAX_CHARS", raising=False)
    assert oi._span_attr_max_chars() == 65536
    monkeypatch.setenv("OTEL_SPAN_ATTR_MAX_CHARS", "4096")
    assert oi._span_attr_max_chars() == 4096
    monkeypatch.setenv("OTEL_SPAN_ATTR_MAX_CHARS", "not-a-number")
    assert oi._span_attr_max_chars() == 65536


def test_truncate_uses_env_default_when_no_limit(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_SPAN_ATTR_MAX_CHARS", "10")
    out = oi._truncate("z" * 100)  # limit 인자 없음 → env 기본 사용.
    assert out.startswith("z" * 10)
    assert "truncated" in out
