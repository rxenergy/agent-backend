from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.application.prompting.information_need_source import (
    InformationNeedPromptSource,
)
from app.application.prompting.local_source import PromptRegistryError

# 실제 repo prompts/ — registry sha 핀이 디스크 fragment 와 일치하는지(boot fail-fast
# 불변식) 단위에서 검증. 프롬프트가 코드 인라인이 아니라 registry 관리임을 강제한다.
_REAL_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def test_real_registry_information_need_prompt_sha_matches_disk() -> None:
    src = InformationNeedPromptSource(_REAL_PROMPTS_DIR)
    # 프롬프트 본문 placeholder + 도메인 슬롯 어휘가 코드가 아니라 fragment 에 있다.
    assert "{query}" in src.prompt_body
    assert "{intent}" in src.prompt_body
    assert "governing_clause" in src.prompt_body
    # output_schema 가 dict 로 파싱돼 grammar 에 쓸 수 있다.
    assert src.schema.get("required", []) == ["required_slots"]
    assert src.model_options.get("max_tokens")  # model_options 외부화
    assert len(src.policy_hash) == 16


def test_missing_block_fails_fast(tmp_path: Path) -> None:
    (tmp_path / "registry.yaml").write_text("prompt_profiles: {}\n", encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        InformationNeedPromptSource(tmp_path)


def test_sha_mismatch_fails_fast(tmp_path: Path) -> None:
    # 무단 편집(프롬프트 바이트 변경, sha 미갱신)은 boot 에서 거부된다.
    (tmp_path / "query_understanding").mkdir()
    (tmp_path / "query_understanding" / "schemas").mkdir()
    prompt = tmp_path / "query_understanding" / "p.md"
    prompt.write_text("tampered {query}", encoding="utf-8")
    schema = tmp_path / "query_understanding" / "schemas" / "s.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    (tmp_path / "registry.yaml").write_text(
        yaml.safe_dump({
            "query_understanding_prompts": {
                "information_need_v1": {
                    "profile_version": "v1",
                    "prompt": {"path": "query_understanding/p.md", "version": "v1",
                               "sha256": "0" * 64},  # 의도적 불일치
                    "output_schema": {"path": "query_understanding/schemas/s.json",
                                      "version": "v1", "sha256": "0" * 64},
                    "model_options": {"temperature": 0.0},
                }
            }
        }),
        encoding="utf-8",
    )
    with pytest.raises(PromptRegistryError):
        InformationNeedPromptSource(tmp_path)
