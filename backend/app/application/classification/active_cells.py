from __future__ import annotations

# v2 MVP phase에서 답변을 허용하는 (object, depth) 셀.
# 기획 doc §3 Top Priority + 기획 doc §7 "비활성 셀" 분기.
#
# 모드:
#   "all"          — 12개 셀 모두 active. 품질 게이트는 verification이 잡는다.
#   "top_priority" — 기획 §3 Top Priority 셀만 active. 그 외는
#                    RefusalReason.UNSUPPORTED_SCENARIO 분기를 타고 §7
#                    "현재 단계에서는 이 유형 답변 제한적" 메시지가 노출된다.
#
# 모드 선택은 settings.active_cells_mode (env ACTIVE_CELLS_MODE) 가 결정.

ALL_CELLS: frozenset[tuple[str, str]] = frozenset(
    (o, d)
    for o in ("O1", "O2", "O3", "O4")
    for d in ("D1", "D2", "D3")
)

TOP_PRIORITY_CELLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("O1", "D2"),  # "NuScale의 PCS 설계 특징은?"
        ("O4", "D2"),  # "NuScale이 RG 1.157을 어떻게 만족?"
        ("O3", "D2"),  # "DWO 관련 RAI들은 무엇을 다뤘나?"
        ("O2", "D3"),  # "RG 1.157의 요건 원문은?"
    }
)


def is_active(scenario_object: str, scenario_depth: str, mode: str = "all") -> bool:
    cells = TOP_PRIORITY_CELLS if mode == "top_priority" else ALL_CELLS
    return (scenario_object, scenario_depth) in cells
