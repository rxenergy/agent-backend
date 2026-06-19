from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.application.prompting.local_source import PromptRegistryError


class _SpecDrivenPromptSource:
    """spec_driven_v1 프롬프트 로더 base — AnswerSpecPromptSource / _ReactPromptSource
    와 동형 fail-fast.

    boot 시 `<registry_key>.<profile_id>` 의 prompt fragment(및 선택적 output_schema)
    바이트를 재해시해 declared sha 와 대조하고 불일치면 `PromptRegistryError`(무단 편집
    차단, 원칙 5). `policy_hash` 는 prompt 본문 sha16 — InteractionEvent 핀이 "어떤
    프롬프트 정책이 이 산출을 만들었나"를 단독 설명한다.

    `has_schema=True` 인 노드(N1/N2)는 json_schema guided decoding 용 스키마를 로드한다.
    N4 Generation 은 자유 텍스트라 스키마가 없다(`has_schema=False`)."""

    source_id = "local"
    registry_key: str = ""
    has_schema: bool = False

    def __init__(self, prompt_dir: str | Path, *, profile_id: str) -> None:
        self._dir = Path(prompt_dir)
        self._profile_id = profile_id
        self.prompt_version: str = "v1"
        self.prompt_body: str = ""
        self.model_options: dict[str, Any] = {}
        self.schema: dict[str, Any] = {}
        self.policy_hash: str = ""
        self._load()

    def _load(self) -> None:
        registry_file = self._dir / "registry.yaml"
        if not registry_file.exists():
            raise PromptRegistryError(f"registry.yaml not found under {self._dir}")
        data = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
        block = (data.get(self.registry_key) or {}).get(self._profile_id)
        if not block:
            raise PromptRegistryError(
                f"registry.yaml has no {self.registry_key}.{self._profile_id}"
            )
        self.prompt_version = str(block.get("profile_version") or "v1")
        self.model_options = dict(block.get("model_options") or {})
        self.prompt_body = self._read_verified(block, "prompt")
        if self.has_schema:
            schema_text = self._read_verified(block, "output_schema")
            try:
                self.schema = json.loads(schema_text)
            except json.JSONDecodeError as e:
                raise PromptRegistryError(
                    f"{self.registry_key}.{self._profile_id} output_schema "
                    f"is not valid JSON: {e}"
                ) from e
        self.policy_hash = hashlib.sha256(
            self.prompt_body.encode("utf-8")
        ).hexdigest()[:16]

    def _read_verified(self, block: dict[str, Any], name: str) -> str:
        ref = block.get(name)
        if not ref or not ref.get("path") or not ref.get("sha256"):
            raise PromptRegistryError(
                f"{self.registry_key}.{self._profile_id} {name!r} requires path + sha256"
            )
        full = self._dir / ref["path"]
        if not full.exists():
            raise PromptRegistryError(
                f"spec_driven prompt {name!r}: file not found at {full}"
            )
        content = full.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != ref["sha256"]:
            raise PromptRegistryError(
                f"spec_driven prompt {name!r} sha mismatch at {ref['path']}: "
                f"declared={ref['sha256'][:12]}…, actual={actual[:12]}… "
                "(bump fragment version + update registry.yaml)"
            )
        return content.decode("utf-8")


class SpecDrivenTriageSource(_SpecDrivenPromptSource):
    """N0 Triage Node 프롬프트 source(json_schema guided — route 판정)."""

    registry_key = "spec_driven_triage_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_triage_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class SpecDrivenGeneralSource(_SpecDrivenPromptSource):
    """N4-G General Generation 프롬프트 source(자유 텍스트 — 스키마 없음).

    RAG 비대상 도메인 질의를 모델 추론으로 직답할 때의 시스템 프롬프트. 범위 한정 날조
    가드(특정 조문 원문·정량값·개정판·신청자 주장 hard-forbid)를 담는다."""

    registry_key = "spec_driven_general_prompts"
    has_schema = False

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_general_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class SpecDrivenAnswerSpecSource(_SpecDrivenPromptSource):
    """N1 Define Spec Node 프롬프트 source(json_schema guided)."""

    registry_key = "spec_driven_answer_spec_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_answer_spec_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class SpecDrivenQuerySource(_SpecDrivenPromptSource):
    """N2 Query Formulation Node 프롬프트 source(json_schema guided)."""

    registry_key = "spec_driven_query_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_query_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class SpecDrivenGenerationSource(_SpecDrivenPromptSource):
    """N4 Generation 프롬프트 source(자유 텍스트 — 스키마 없음)."""

    registry_key = "spec_driven_generation_prompts"
    has_schema = False

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_generation_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class ComposerSlotSource(_SpecDrivenPromptSource):
    """composer N4.1 슬롯 생성 프롬프트 source(자유 텍스트). 슬롯 1개를 facet 범위로
    펼치고 이전 슬롯 요지(PRIOR SECTIONS)를 참조한다. 설계:
    docs/plans/spec_driven_slotwise_generation.design.v1.md §6.1."""

    registry_key = "composer_slot_prompts"
    has_schema = False

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "composer_slot_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class ComposerSynthesizeSource(_SpecDrivenPromptSource):
    """composer N4.3 종합 프롬프트 source(자유 텍스트). 슬롯 본문 재출력 금지 — "핵심
    정리 + 다음 단계 제안" 닫음 블록만(슬롯은 조기 스트리밍됨). 설계 §5/§6.2."""

    registry_key = "composer_synthesize_prompts"
    has_schema = False

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "composer_synthesize_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class ComposerSlotVerifySource(_SpecDrivenPromptSource):
    """composer N4.2 L1 groundedness 게이트 프롬프트 source(json_schema guided). 슬롯
    출력↔CONTEXT entailment 판정만(생성 아님 — self-verification 금지). 설계 §4.1."""

    registry_key = "composer_slot_verify_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "composer_slot_verify_v1") -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


# composer N1/N2/슬롯 v2 — 책임 재분배(answer_spec_query_responsibility_split.design.v1).
# composer 만 이 source 를 쓴다(spec_driven_v1/v2 variant 의 N1/N2 source 와 별개 — A/B
# 비교 위해 base profile 불변). N1 은 검색 지식을 뺀 답변 설계 프롬프트+v2 스키마, N2 는
# address map 을 흡수한 검색 설계 프롬프트(출력 스키마는 v1 동형), 슬롯은 role/depends_on
# 소비·헤더 충돌 해소판. base 로직(sha 검증·schema 로드) 그대로, profile_id 만 분리.
class ComposerAnswerSpecSource(SpecDrivenAnswerSpecSource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="composer_answer_spec_v1")


class ComposerQuerySource(SpecDrivenQuerySource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="composer_query_v1")


class ComposerSlotV2Source(ComposerSlotSource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="composer_slot_v2")


# composer 다중 페르소나(composer_persona_framework.design.v1 §10) — 페르소나 프로필
# fragment source(자유 텍스트, 스키마 없음). profile_id 는 Persona.profile_source_id 와
# 일치(composer_{persona_id} variant 가 자기 페르소나의 fragment 를 조회). prompt_body 가
# N1/N2/N4 프롬프트 앞에 `# PERSONA` 블록으로 prepend 된다(단일 fragment, 세 노드).
class ComposerPersonaSource(_SpecDrivenPromptSource):
    registry_key = "composer_persona_prompts"
    has_schema = False

    def __init__(self, prompt_dir: str | Path, *, profile_id: str) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


# spec_driven_v2 — 2-노드(DGX Spark) 분산 변형 전용 프롬프트 source. base 로직은 동일하고
class SpecDrivenVerifySource(_SpecDrivenPromptSource):
    """composer_pipelined Node2 — 슬롯 단위 검증 프롬프트 source(json_schema guided).
    슬롯 1개의 청크 전체를 한 프롬프트로 합쳐 단일 호출 → 필요 청크 + 멀티홉 청크 식별자
    리스트 산출(verify_slot_v2). secondary_llm(Node2 = sub)에서 호출된다."""

    registry_key = "spec_driven_verify_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_verify_v2"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)


class SpecDrivenRescopeSource(_SpecDrivenPromptSource):
    """spec_driven_v2 retrieval.rescope — none_necessary 슬롯의 스코프 재계획 프롬프트
    source(json_schema guided). verify_slot 이 none_necessary 판정 시, opinion + 1차
    planning 스코프를 받아 검색 스코프를 새로 잡는다(planning 스코프 어휘 재사용).
    secondary_llm(Node2)에서 호출된다 — verify/follow_up 과 같은 노드."""

    registry_key = "spec_driven_rescope_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_rescope_v1"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)
