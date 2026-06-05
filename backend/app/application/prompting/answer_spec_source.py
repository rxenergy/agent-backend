from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.application.intake.answer_spec import AnswerSpecInstantiator
from app.application.prompting.local_source import PromptRegistryError
from app.ports.llm import LLMPort


class AnswerSpecPromptSource:
    """Loads the agentic_finder N2 answer-spec prompt from `prompts/registry.yaml`.

    `ClassificationPromptSource`/`InformationNeedPromptSource` 와 동형 — boot 시
    `answer_spec_prompts.<id>` 의 prompt fragment + output schema 바이트를 재해시해
    declared sha 와 대조하고 불일치면 `PromptRegistryError` 로 fail-fast(무단 편집
    차단, 원칙 5). 프롬프트는 코드 인라인이 아니라 registry 에서 관리된다.

    `policy_hash` 는 prompt 본문의 sha16 — classifier_policy_hash 와 동일 idiom 이라
    InteractionEvent 의 `answer_spec_hash` 핀(F-6 승격)이 "어떤 프롬프트 정책이 이
    사양을 산출했나"를 단독 설명한다. `schema` 는 json_schema grammar(guided)에 쓴다."""

    source_id = "local"

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "answer_spec_v1"
    ) -> None:
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
        block = (data.get("answer_spec_prompts") or {}).get(self._profile_id)
        if not block:
            raise PromptRegistryError(
                f"registry.yaml has no answer_spec_prompts.{self._profile_id}"
            )
        self.prompt_version = str(block.get("profile_version") or "v1")
        self.model_options = dict(block.get("model_options") or {})
        self.prompt_body = self._read_verified(block, "prompt")
        schema_text = self._read_verified(block, "output_schema")
        try:
            self.schema = json.loads(schema_text)
        except json.JSONDecodeError as e:
            raise PromptRegistryError(
                f"answer_spec_prompts.{self._profile_id} output_schema "
                f"is not valid JSON: {e}"
            ) from e
        self.policy_hash = hashlib.sha256(
            self.prompt_body.encode("utf-8")
        ).hexdigest()[:16]

    def _read_verified(self, block: dict[str, Any], name: str) -> str:
        ref = block.get(name)
        if not ref or not ref.get("path") or not ref.get("sha256"):
            raise PromptRegistryError(
                f"answer_spec_prompts.{self._profile_id} {name!r} requires path + sha256"
            )
        full = self._dir / ref["path"]
        if not full.exists():
            raise PromptRegistryError(
                f"answer-spec prompt {name!r}: file not found at {full}"
            )
        content = full.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != ref["sha256"]:
            raise PromptRegistryError(
                f"answer-spec prompt {name!r} sha mismatch at {ref['path']}: "
                f"declared={ref['sha256'][:12]}…, actual={actual[:12]}… "
                "(bump fragment version + update registry.yaml)"
            )
        return content.decode("utf-8")

    def build_instantiator(self, llm: LLMPort) -> AnswerSpecInstantiator:
        return AnswerSpecInstantiator(
            llm,
            prompt_body=self.prompt_body,
            schema=self.schema,
            model_options=self.model_options,
            policy_hash=self.policy_hash,
        )
