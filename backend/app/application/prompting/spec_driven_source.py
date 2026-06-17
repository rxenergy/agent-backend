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


# spec_driven_v2 — 2-노드(DGX Spark) 분산 변형 전용 프롬프트 source. base 로직은 동일하고
# 기본 profile_id 만 `*_v2` 로 바꿔 registry 의 v2 블록을 읽는다. 초기 v2 블록은 v1 fragment
# 를 그대로 참조(동일 sha)하므로 동작은 v1 과 같으나, v2 전용 프롬프트 진화를 v1 과 격리한다
# (설계 spec_driven_agent.design.v2). policy_hash 는 본문 sha16 이라 동일 fragment 면 v1==v2.


class SpecDrivenTriageV2Source(SpecDrivenTriageSource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="spec_driven_triage_v2")


class SpecDrivenGeneralV2Source(SpecDrivenGeneralSource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="spec_driven_general_v2")


class SpecDrivenAnswerSpecV2Source(SpecDrivenAnswerSpecSource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="spec_driven_answer_spec_v2")


class SpecDrivenQueryV2Source(SpecDrivenQuerySource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="spec_driven_query_v2")


class SpecDrivenGenerationV2Source(SpecDrivenGenerationSource):
    def __init__(self, prompt_dir: str | Path) -> None:
        super().__init__(prompt_dir, profile_id="spec_driven_generation_v2")


class SpecDrivenVerifySource(_SpecDrivenPromptSource):
    """spec_driven_v2 Node2 — 슬롯 단위 검증 프롬프트 source(json_schema guided).
    필요 청크 + 멀티홉 청크 식별자 산출. SECONDARY_LLM(Node2)에서 호출된다."""

    registry_key = "spec_driven_verify_prompts"
    has_schema = True

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "spec_driven_verify_v2"
    ) -> None:
        super().__init__(prompt_dir, profile_id=profile_id)
