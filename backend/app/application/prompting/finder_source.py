from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.application.prompting.local_source import PromptRegistryError


class FinderPromptSource:
    """Loads the agentic_finder N3 Finder system prompt from `prompts/registry.yaml`.

    분류/정보요구/answer_spec source 와 동형 fail-fast sha 검증 — boot 시
    `finder_prompts.<id>` 의 prompt fragment 바이트를 재해시해 declared sha 와 대조하고
    불일치면 `PromptRegistryError`(무단 편집 차단, 원칙 5). 다른 source 와 달리
    output_schema 가 없다 — Finder 의 구조는 자유 텍스트가 아니라 도구 호출
    (submit_verdict)이 강제한다(structured-by-construction).

    `policy_hash` 는 prompt 본문 sha16 — InteractionEvent 의 `finder_policy_hash`
    핀이 "어떤 Finder 정책이 이 검색 루프를 돌렸나"를 단독 설명한다."""

    source_id = "local"

    def __init__(
        self, prompt_dir: str | Path, *, profile_id: str = "finder_v1"
    ) -> None:
        self._dir = Path(prompt_dir)
        self._profile_id = profile_id
        self.prompt_version: str = "v1"
        self.prompt_body: str = ""
        self.model_options: dict[str, Any] = {}
        self.policy_hash: str = ""
        self._load()

    def _load(self) -> None:
        registry_file = self._dir / "registry.yaml"
        if not registry_file.exists():
            raise PromptRegistryError(f"registry.yaml not found under {self._dir}")
        data = yaml.safe_load(registry_file.read_text(encoding="utf-8")) or {}
        block = (data.get("finder_prompts") or {}).get(self._profile_id)
        if not block:
            raise PromptRegistryError(
                f"registry.yaml has no finder_prompts.{self._profile_id}"
            )
        self.prompt_version = str(block.get("profile_version") or "v1")
        self.model_options = dict(block.get("model_options") or {})
        self.prompt_body = self._read_verified(block, "prompt")
        self.policy_hash = hashlib.sha256(
            self.prompt_body.encode("utf-8")
        ).hexdigest()[:16]

    def _read_verified(self, block: dict[str, Any], name: str) -> str:
        ref = block.get(name)
        if not ref or not ref.get("path") or not ref.get("sha256"):
            raise PromptRegistryError(
                f"finder_prompts.{self._profile_id} {name!r} requires path + sha256"
            )
        full = self._dir / ref["path"]
        if not full.exists():
            raise PromptRegistryError(
                f"finder prompt {name!r}: file not found at {full}"
            )
        content = full.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != ref["sha256"]:
            raise PromptRegistryError(
                f"finder prompt {name!r} sha mismatch at {ref['path']}: "
                f"declared={ref['sha256'][:12]}…, actual={actual[:12]}… "
                "(bump fragment version + update registry.yaml)"
            )
        return content.decode("utf-8")
