from __future__ import annotations

from app.api.openai_compat import _strip_task_scaffolding

# OpenWebUI follow-up 생성 task 의 실제 페이로드 형태 (지시문 + <chat_history>).
_FOLLOWUP_PROMPT = """### Task:
Suggest 3-5 relevant follow-up questions based on the chat history.
### Guidelines:
- Write all follow-up questions from the user's point of view.
### Output:
JSON format: { "follow_ups": ["Question 1?"] }
### Chat History:
<chat_history>
USER: GDC 35(비상 노심 냉각)의 기술적 요건은 무엇인가?
ASSISTANT: 1. 규제 식별 — 10 CFR 50 Appendix A, GDC 35
2. 원문 인용
> A system to provide abundant emergency core cooling shall be provided. [cite-0]
</chat_history>"""


def test_normal_query_is_passthrough():
    """서명(<chat_history>)이 없는 일반 질의는 원문 그대로 (no-op)."""
    q = "RG 1.157의 요건 원문은 무엇인가?"
    assert _strip_task_scaffolding(q) == q


def test_followup_task_reduces_to_last_user_turn():
    """task 메타 프롬프트는 지시문·답변 전문을 버리고 마지막 USER 발화로 환원."""
    out = _strip_task_scaffolding(_FOLLOWUP_PROMPT)
    assert out == "GDC 35(비상 노심 냉각)의 기술적 요건은 무엇인가?"
    # 지시문 / 직전 답변 / 인용 토큰이 검색 질의에서 사라져야 한다.
    assert "follow_ups" not in out
    assert "abundant emergency core cooling" not in out
    assert "### Task" not in out


def test_picks_last_user_turn_when_multiple():
    text = (
        "<chat_history>\n"
        "USER: 첫 번째 질문\n"
        "ASSISTANT: 첫 번째 답변\n"
        "USER: 두 번째 질문\n"
        "ASSISTANT: 두 번째 답변\n"
        "</chat_history>"
    )
    assert _strip_task_scaffolding(text) == "두 번째 질문"


def test_multiline_user_turn_is_preserved():
    text = (
        "<chat_history>\n"
        "USER: 질문 첫 줄\n"
        "둘째 줄도 같은 질문\n"
        "ASSISTANT: 답변\n"
        "</chat_history>"
    )
    assert _strip_task_scaffolding(text) == "질문 첫 줄\n둘째 줄도 같은 질문"


def test_no_user_turn_falls_back_to_history_body():
    text = "<chat_history>\nSYSTEM: 시스템 안내\nASSISTANT: 답변만 있음\n</chat_history>"
    out = _strip_task_scaffolding(text)
    assert "ASSISTANT: 답변만 있음" in out
    assert "<chat_history>" not in out


def test_empty_chat_history_falls_back_to_original():
    text = "prefix <chat_history>\n\n</chat_history> suffix"
    # 빈 블록이면 환원할 게 없으므로 원문 유지.
    assert _strip_task_scaffolding(text) == text
