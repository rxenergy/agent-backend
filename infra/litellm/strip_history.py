# LiteLLM pre-call guard — Bedrock 직결 경로의 대화 이력 차단.
#
# 왜 필요한가:
#   OpenWebUI 멀티모델 비교(2+ 모델 동시 호출)에서, follow-up turn 의
#   `messages` 는 parentId 체인만 따라 직선 재구성된다 (modelIdx 필터 없음).
#   그 결과 직전 turn 에서 *마지막에* 응답한 모델(예: 온프레 agent-api)의
#   답변이 다음 turn 의 모든 모델 history 에 assistant 로 끼어 들어간다 →
#   온프레 답변이 Bedrock(Claude)으로 raw 송신되는 거버넌스 누수.
#   OpenWebUI 에는 per-model history 격리 옵션이 없으므로(소스 확인), 프록시
#   경계에서 강제로 끊는다.
#
# 동작:
#   Bedrock 호출 직전 messages 를 system + 마지막 user turn 만 남기고 비운다.
#   → 이 경로는 완전 single-turn (의도된 trade-off; 비교/직결 용도, 멀티턴 불가).
#   assistant 이력 전체가 제거되므로 다른 모델 답변뿐 아니라 Claude 자신의
#   직전 답변도 보이지 않는다. 멀티턴 맥락이 필요하면 온프레 agent-api 경로를
#   쓴다 (그쪽이 재현 가능한 실행 + SessionState 를 보장).
#
# raw passthrough 원칙(design v1)은 유지: 에이전트 로직/RAG/검증 없음, 단지
# 외부 송신 경계의 거버넌스 가드.

from litellm.integrations.custom_logger import CustomLogger


class StripHistory(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # chat 계열 호출만 대상 (embedding 등은 messages 가 없다).
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return data

        # system 프롬프트는 전부 보존(순서 유지) — 모델 행동 지시이지 대화 이력 아님.
        system = [m for m in messages if m.get("role") == "system"]

        # 마지막 user turn 만 남긴다. tail 부터 역순으로, user 를 만날 때까지
        # 모은 뒤 다시 정방향으로 뒤집는다 (멀티파트 user 메시지 대비).
        tail = []
        for m in reversed(messages):
            role = m.get("role")
            if role == "user":
                tail.append(m)
                break
            if role == "system":
                # 이미 system 묶음에 포함 — tail 에 중복 추가하지 않는다.
                continue
            # assistant / tool 등 직전 답변 이력은 버린다.
        tail.reverse()

        data["messages"] = system + tail
        return data


proxy_handler_instance = StripHistory()
