from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.application.classification.llm import LLMClassifier
from app.application.prompting.local_source import PromptRegistryError
from app.ports.llm import LLMPort


class ClassificationPromptSource:
    """Loads the Node 1 classifier prompt from `prompts/registry.yaml`.

    Mirrors `LocalPromptSource`'s fail-fast sha invariant (spec §4.5): on
    construction the referenced fragment + output schema are read and their
    on-disk bytes SHA256'd against the values declared under the
    `classification_prompts.<id>` registry block. A mismatch raises
    `PromptRegistryError` — silently editing the classifier prompt without
    bumping the registry is rejected at boot. This is the *delta* the registry
    hosting buys over the prior inline `_PROMPT`: boot-time verification +
    co-location with answer prompts + externalized model_options.

    `policy_hash` is the prompt body's sha16 — identical idiom to the prior
    inline pin, so `InteractionEvent.classifier_policy_hash` stays comparable
    across the move (only the source changed, not the pin semantics).
    """

    source_id = "local"

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "classifier_v1"
    ) -> None:
        self._dir = Path(prompt_dir)
        self._profile_id = profile_id
        self.prompt_version: str = "v1"
        self.prompt_body: str = ""
        self.model_options: dict[str, Any] = {}
        self.output_schema: str = ""
        self.policy_hash: str = ""
        self._load()

    def _load(self) -> None:
        registry_file = self._dir / "registry.yaml"
        if not registry_file.exists():
            raise PromptRegistryError(f"registry.yaml not found under {self._dir}")
        data = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
        block = (data.get("classification_prompts") or {}).get(self._profile_id)
        if not block:
            raise PromptRegistryError(
                f"registry.yaml has no classification_prompts.{self._profile_id}"
            )
        self.prompt_version = str(block.get("profile_version") or "v1")
        self.model_options = dict(block.get("model_options") or {})
        self.prompt_body = self._read_verified(block, "prompt")
        self.output_schema = self._read_verified(block, "output_schema")
        # 정책 핀 = 프롬프트 본문 sha16 (인라인 시절과 동일 idiom).
        self.policy_hash = hashlib.sha256(
            self.prompt_body.encode("utf-8")
        ).hexdigest()[:16]

    def _read_verified(self, block: dict[str, Any], name: str) -> str:
        ref = block.get(name)
        if not ref or not ref.get("path") or not ref.get("sha256"):
            raise PromptRegistryError(
                f"classification_prompts.{self._profile_id} {name!r} requires path + sha256"
            )
        full = self._dir / ref["path"]
        if not full.exists():
            raise PromptRegistryError(
                f"classification prompt {name!r}: file not found at {full}"
            )
        content = full.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != ref["sha256"]:
            raise PromptRegistryError(
                f"classification prompt {name!r} sha mismatch at {ref['path']}: "
                f"declared={ref['sha256'][:12]}…, actual={actual[:12]}… "
                "(bump fragment version + update registry.yaml)"
            )
        return content.decode("utf-8")

    def build_classifier(self, llm: LLMPort) -> LLMClassifier:
        return LLMClassifier(
            llm,
            prompt_body=self.prompt_body,
            model_options=self.model_options,
            policy_hash=self.policy_hash,
        )
