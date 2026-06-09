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
