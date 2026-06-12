"""환경 변수 로딩. chunking 패키지 내부에서 자족적으로 동작한다.

CLI 인자가 우선이고, 미지정 시 환경 변수 → 코드 기본값 순으로 사용된다.

추출 knob(max_output_tokens 등)만 담는다. LLM 연결(endpoint/model/api_key/
timeout·재시도)은 더 이상 여기서 다루지 않는다 — 메인 경로와 동일하게 ``LLM_POOL``
로 구성된 :class:`~app.ports.llm.LLMPort`(HttpLLM)가 단독 소유한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


DEFAULT_MAX_RETRIES = 5
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_MAX_OUTPUT_TOKENS_WITH_FOLLOW_UP = 2048
DEFAULT_BACKEND: Literal["vllm"] = "vllm"
DEFAULT_MAX_TOOL_TURNS = 32

# metadata_schema.md는 본 패키지 디렉토리에 함께 위치
_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SCHEMA_MD_PATH = _PACKAGE_DIR / "metadata_schema.md"

# 데이터 소스 기본 경로.
DEFAULT_METADATA_CSV_REL = "metadata_unified.csv"


@dataclass(frozen=True)
class RefSettings:
    max_retries: int
    max_output_tokens: int
    backend: Literal["vllm"]
    schema_md_path: Path
    max_tool_turns: int

    @classmethod
    def from_env(
        cls,
        *,
        backend: str | None = None,
        max_tool_turns: int | None = None,
    ) -> "RefSettings":
        resolved_backend = _resolve_backend(
            backend or os.environ.get("DOCUMENTS_REF_BACKEND", DEFAULT_BACKEND)
        )

        schema_md_env = os.environ.get("DOCUMENTS_REF_SCHEMA_MD")
        schema_md_path = Path(schema_md_env) if schema_md_env else DEFAULT_SCHEMA_MD_PATH

        return cls(
            max_retries=int(os.environ.get("DOCUMENTS_REF_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            max_output_tokens=int(
                os.environ.get("DOCUMENTS_REF_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)
            ),
            backend=resolved_backend,
            schema_md_path=schema_md_path,
            max_tool_turns=max_tool_turns
            or int(os.environ.get("DOCUMENTS_REF_MAX_TOOL_TURNS", DEFAULT_MAX_TOOL_TURNS)),
        )


def _resolve_backend(value: str) -> Literal["vllm"]:
    v = (value or "").strip().lower()
    if v == "vllm":
        return "vllm"
    raise ValueError(f"invalid backend {value!r} (expected 'vllm')")
