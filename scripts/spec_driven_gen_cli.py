#!/usr/bin/env python3
"""spec_driven_v1 N4 Generation 을 onprem vLLM 에 *직접* 던지는 CLI 실험 도구.

런너(SpecDrivenRunner._generate)는 N4 에서 OpenAI-compat `/v1/chat/completions` 로
**user 메시지 하나**(system 메시지 없음)에 합성 프롬프트 전문을 실어 보낸다. 합성식은
SpecDrivenRunner._render_generation_prompt 와 동일:

    <generation_v1.md body>
    [# CITATION CONTRACT <...>]        (선택 — citation_contract_path 설정 시)
    # ANSWER SPEC\n<spec block>        (이 CLI 는 자유 입력 — 생략 가능)
    # CONTEXT\n<rendered chunks>        (이 CLI 는 자유 입력 — 생략 가능)
    [# EVIDENCE GAP (NO RESULTS)\n<...>] (--gap 플래그)
    # QUERY\n<원질의>
    # RESPONSE LANGUAGE\n<출력 언어 trailer>

model_options 는 registry(spec_driven_generation_v1)와 동일:
    temperature=0.1, top_p=0.9, max_tokens=16384, seed=7

전체 프롬프트 본문(generation_v1.md)을 자동 prepend 하므로, CLI 에서는 ANSWER SPEC /
CONTEXT / QUERY 만 입력하면 런너가 vLLM 에 실제로 보내는 것과 동일한 페이로드가 된다.
프롬프트 본문 없이 *날것* 만 보내려면 --no-body.

사용:
    # QUERY 만 직접 입력 (가장 단순)
    python scripts/spec_driven_gen_cli.py --query "10 CFR 50.34 의 PSAR 요건은?"

    # CONTEXT(검색 청크 본문)를 파일로 동봉
    python scripts/spec_driven_gen_cli.py --query "..." --context-file chunks.txt

    # 근거 0건(gap-answer) 경로 재현
    python scripts/spec_driven_gen_cli.py --query "..." --gap

    # 합성된 프롬프트를 *보내지 않고* 출력만 (실제 curl 페이로드 확인)
    python scripts/spec_driven_gen_cli.py --query "..." --dry-run

    # 스트리밍 (vLLM 토큰 SSE)
    python scripts/spec_driven_gen_cli.py --query "..." --stream

기본 엔드포인트/모델은 onprem.env 의 default LLM(gemma-4-26b)과 동일.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

# 런너가 N4 에 합성하는 기본 model_options (prompts/registry.yaml
# spec_driven_generation_prompts.spec_driven_generation_v1.model_options).
_DEFAULT_OPTS = {"temperature": 0.1, "top_p": 0.9, "max_tokens": 16384, "seed": 7}

# 런너가 # QUERY 뒤에 붙이는 출력-언어 trailer (최고 recency).
_LANG_TRAILER = (
    "Write the final answer in the same language as the QUERY above "
    "(Korean query → Korean answer). Citation markers and source ids stay verbatim."
)

# 0-chunk hard-forbid 블록 (_render_generation_prompt 의 EVIDENCE GAP 와 동일 문구).
_GAP_BLOCK = (
    "Retrieval found no evidence at all. Do not fabricate regulatory facts "
    "from prior knowledge or memory (and do not use citation markers — there is "
    "no evidence). State only: (1) which explicit references / keywords you "
    "searched, (2) what you could not verify, (3) what more is needed for a "
    "defensible answer. State explicitly that confidence is low."
)

_REPO = Path(__file__).resolve().parent.parent
_BODY_PATH = _REPO / "prompts" / "spec_driven" / "generation_v1.md"


def _read(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def _build_prompt(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if not args.no_body:
        parts.append(_BODY_PATH.read_text(encoding="utf-8").strip())
    contract = _read(args.citation_contract)
    if contract:
        parts.append("# CITATION CONTRACT\n" + contract.strip())
    spec = _read(args.spec_file) or args.spec
    if spec:
        parts.append("# ANSWER SPEC\n" + spec.strip())
    context = _read(args.context_file) or args.context
    if context:
        parts.append("# CONTEXT\n" + context.strip())
    if args.gap:
        parts.append("# EVIDENCE GAP (NO RESULTS)\n" + _GAP_BLOCK)
    parts.append("# QUERY\n" + args.query)
    parts.append("# RESPONSE LANGUAGE\n" + _LANG_TRAILER)
    return "\n\n".join(parts)


def _post(url: str, payload: dict, api_key: str | None, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_stream(url: str, payload: dict, api_key: str | None, timeout: float) -> None:
    payload = {**payload, "stream": True}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            # reasoning(있으면)·content 둘 다 흘린다 — gemma 는 보통 content 만.
            if delta.get("reasoning_content"):
                sys.stderr.write(delta["reasoning_content"])
                sys.stderr.flush()
            if delta.get("content"):
                sys.stdout.write(delta["content"])
                sys.stdout.flush()
    sys.stdout.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", "-q", required=True, help="원질의 (# QUERY 블록)")
    ap.add_argument("--spec", help="ANSWER SPEC 블록 본문(인라인)")
    ap.add_argument("--spec-file", help="ANSWER SPEC 블록을 파일에서")
    ap.add_argument("--context", help="CONTEXT(검색 청크) 본문(인라인)")
    ap.add_argument("--context-file", help="CONTEXT 를 파일에서")
    ap.add_argument("--citation-contract", help="CITATION CONTRACT 파일 경로")
    ap.add_argument("--gap", action="store_true",
                    help="EVIDENCE GAP(0-chunk hard-forbid) 블록 주입")
    ap.add_argument("--no-body", action="store_true",
                    help="generation_v1.md 시스템 본문 prepend 생략(날것만)")

    ap.add_argument("--endpoint", default="http://vllm:8000/v1",
                    help="OpenAI-compat 베이스 URL (기본 onprem 메인 vLLM)")
    ap.add_argument("--model", default="gemma-4-26b-a4b-it",
                    help="served model name (기본 onprem gemma-4)")
    ap.add_argument("--api-key", default=None, help="Bearer 토큰(vLLM 은 보통 불필요)")
    ap.add_argument("--timeout", type=float, default=600.0)

    ap.add_argument("--temperature", type=float, default=_DEFAULT_OPTS["temperature"])
    ap.add_argument("--top-p", type=float, default=_DEFAULT_OPTS["top_p"])
    ap.add_argument("--max-tokens", type=int, default=_DEFAULT_OPTS["max_tokens"])
    ap.add_argument("--seed", type=int, default=_DEFAULT_OPTS["seed"])

    ap.add_argument("--stream", action="store_true", help="SSE 스트리밍")
    ap.add_argument("--dry-run", action="store_true",
                    help="보내지 않고 합성된 프롬프트/페이로드만 출력")
    args = ap.parse_args()

    prompt = _build_prompt(args)
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "seed": args.seed,
    }

    if args.dry_run:
        sys.stderr.write("===== RENDERED PROMPT =====\n")
        sys.stdout.write(prompt + "\n")
        sys.stderr.write("\n===== PAYLOAD (messages.content elided) =====\n")
        elided = {**payload, "messages": [{"role": "user",
                  "content": f"<{len(prompt)} chars>"}]}
        sys.stderr.write(json.dumps(elided, ensure_ascii=False, indent=2) + "\n")
        return 0

    url = args.endpoint.rstrip("/") + "/chat/completions"
    if args.stream:
        _post_stream(url, payload, args.api_key, args.timeout)
        return 0

    data = _post(url, payload, args.api_key, args.timeout)
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or choice.get("text") or ""
    usage = data.get("usage") or {}
    sys.stdout.write(text + "\n")
    sys.stderr.write(
        f"\n[model={data.get('model', args.model)} "
        f"prompt_tokens={usage.get('prompt_tokens', '?')} "
        f"completion_tokens={usage.get('completion_tokens', '?')}]\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
