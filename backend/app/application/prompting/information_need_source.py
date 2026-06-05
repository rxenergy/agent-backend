from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.application.prompting.local_source import PromptRegistryError
from app.application.query_understanding.instantiator import InformationNeedInstantiator
from app.ports.llm import LLMPort


class InformationNeedPromptSource:
    """Loads the Node 3 information-need prompt from `prompts/registry.yaml`.

    `ClassificationPromptSource` 와 동형 — boot 시 `query_understanding_prompts.<id>`
    의 prompt fragment + output schema 바이트를 재해시해 declared sha 와 대조하고
    불일치면 `PromptRegistryError` 로 fail-fast(무단 편집 차단, 원칙 5). 프롬프트가
    더 이상 코드 인라인이 아니라 registry 에서 관리된다.

    `policy_hash` 는 prompt 본문의 sha16 — classifier_policy_hash 와 동일 idiom 이라
    InteractionEvent 에서 "어떤 프롬프트 정책이 이 요구를 산출했나"를 단독 설명한다.
    `output_schema` 는 JSON 으로 파싱해 json_schema grammar(guided decoding)에 쓴다."""

    source_id = "local"

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "information_need_v1"
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
        block = (data.get("query_understanding_prompts") or {}).get(self._profile_id)
        if not block:
            raise PromptRegistryError(
                f"registry.yaml has no query_understanding_prompts.{self._profile_id}"
            )
        self.prompt_version = str(block.get("profile_version") or "v1")
        self.model_options = dict(block.get("model_options") or {})
        self.prompt_body = self._read_verified(block, "prompt")
        schema_text = self._read_verified(block, "output_schema")
        try:
            self.schema = json.loads(schema_text)
        except json.JSONDecodeError as e:
            raise PromptRegistryError(
                f"query_understanding_prompts.{self._profile_id} output_schema "
                f"is not valid JSON: {e}"
            ) from e
        # 정책 핀 = 프롬프트 본문 sha16 (classifier_policy_hash 와 동일 idiom).
        self.policy_hash = hashlib.sha256(
            self.prompt_body.encode("utf-8")
        ).hexdigest()[:16]

    def _read_verified(self, block: dict[str, Any], name: str) -> str:
        ref = block.get(name)
        if not ref or not ref.get("path") or not ref.get("sha256"):
            raise PromptRegistryError(
                f"query_understanding_prompts.{self._profile_id} {name!r} "
                "requires path + sha256"
            )
        full = self._dir / ref["path"]
        if not full.exists():
            raise PromptRegistryError(
                f"information-need prompt {name!r}: file not found at {full}"
            )
        content = full.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != ref["sha256"]:
            raise PromptRegistryError(
                f"information-need prompt {name!r} sha mismatch at {ref['path']}: "
                f"declared={ref['sha256'][:12]}…, actual={actual[:12]}… "
                "(bump fragment version + update registry.yaml)"
            )
        return content.decode("utf-8")

    def build_instantiator(self, llm: LLMPort) -> InformationNeedInstantiator:
        return InformationNeedInstantiator(
            llm,
            prompt_body=self.prompt_body,
            schema=self.schema,
            model_options=self.model_options,
            policy_hash=self.policy_hash,
        )
