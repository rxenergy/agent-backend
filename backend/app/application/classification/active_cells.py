from __future__ import annotations

# v2 MVP phase에서 답변을 허용하는 (object, depth) 셀.
# 기획 doc §3 Top Priority 5셀을 기본으로 하되, generic-only 11셀도 답변은 시도하고
# 품질 게이트는 verification이 잡도록 한다. 완전 비활성으로 두고 싶은 셀이 생기면
# 이 set에서 빼고 RefusalReason.UNSUPPORTED_SCENARIO 분기를 타게 한다.

ACTIVE_CELLS: frozenset[tuple[str, str]] = frozenset(
    (o, d)
    for o in ("O1", "O2", "O3", "O4")
    for d in ("D1", "D2", "D3", "D4")
)


def is_active(scenario_object: str, scenario_depth: str) -> bool:
    return (scenario_object, scenario_depth) in ACTIVE_CELLS
