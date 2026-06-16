#!/usr/bin/env python3
"""유효 컨텍스트(effective context) 측정 하니스 — needle/RULER식 probe.

설계: docs/plans/spec_driven_smallmodel_longcontext.design.v1.md §B.6.

목적: gemma-4-26B-A4B-it(비양자) 및 그 AWQ-4bit 변종이 *공칭* 256K 윈도우 중 실제로
어디까지 신뢰성 있게 attend 하는지를 실측한다. 공식 카드의 유일한 long-context 지표는
MRCR-8needle@128k=44.1% 뿐이고 256K 깊이·RULER·AWQ 영향 데이터가 전무하므로, 200K
컨텍스트 운용을 정당화/기각할 *우리 코퍼스 도메인*의 곡선을 직접 만든다.

방법(spec_driven N4의 실패 모드를 모사):
  - **needle** = 알려진 규제 사실 1건(예: 10 CFR 50.46 PCT 한계 2200°F). 정답은 코드가
    알고 있으므로 채점이 결정론적이다.
  - **filler** = 도메인 유사 잡음 단락(규제풍이되 needle 과 무관) 을 토큰 예산만큼 채운다.
    needle 을 filler 안 *depth*(0.0=맨앞 … 0.5=정중앙 … 1.0=맨뒤) 위치에 끼운다.
  - 프롬프트는 N4와 동형 골격: [지시][CONTEXT(filler+needle)][QUERY]. 모델이 needle 값을
    답에 인용하면 hit, 아니면 miss. → depth × budget 격자의 hit-rate 곡선.
  - lost-in-the-middle / sliding-window(5/6 층 window=1024) 가설은 *중앙 depth*에서 hit-rate
    급락으로 나타난다. AWQ vs 비양자는 같은 격자를 두 endpoint 로 돌려 비교.

연결: Settings(=env)의 LLM_POOL 에서 provider=openai_compat endpoint 를 고른다(앱 배선과
동일). 컨테이너 밖 호스트면 endpoint 의 호스트명을 localhost 로 덮어쓰거나 --endpoint 로 준다.

사용::

    # onprem.env 를 적재한 셸에서(앱과 동일 env), 기본 격자 실행:
    python3 scripts/exp_effective_context.py

    # 격자·반복·엔드포인트 지정:
    python3 scripts/exp_effective_context.py \
        --budgets 4000,16000,64000,128000,200000 \
        --depths 0.0,0.25,0.5,0.75,1.0 \
        --trials 3 \
        --endpoint http://localhost:8000/v1 --model gemma-4-26b-a4b-it \
        --out runs/effctx_awq.jsonl

    # 비양자 vs AWQ 비교: 두 endpoint 로 따로 돌려 --out 을 나눈 뒤 표 비교.

httpx 만 있으면 된다(임베딩/torch 불필요 — 검색을 거치지 않고 vLLM 만 때린다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# host 레이아웃(scripts/ 옆 backend/) / 컨테이너 레이아웃(/app) 모두 지원 — exp_retrieval 와 동일.
_HERE = Path(__file__).resolve().parent.parent
for _cand in (_HERE / "backend", _HERE):
    if (_cand / "app" / "config" / "settings.py").exists():
        sys.path.insert(0, str(_cand))
        break

import httpx  # noqa: E402

# ── needle 사실(채점이 결정론적이도록 코드가 정답을 안다) ────────────────────
# 값 토큰은 BM25 anchor 가 아니라 *모델 인출* 대상이다 — 검색이 아니라 attention 을 본다.
# 정답 판정은 answer 에 needle_answer 의 핵심 토큰이 등장하는지(대소문자·공백 무시)로 한다.
@dataclass
class Needle:
    needle_id: str
    # CONTEXT 안에 심을 사실 문장(filler 와 같은 규제풍 — 형태로 구별되면 attention 이 아니라
    # 표면 패턴을 보게 되므로 의도적으로 동형).
    statement: str
    # 사용자 질의(이 needle 만이 답할 수 있게 구체적으로).
    question: str
    # 정답에 반드시 등장해야 하는 토큰들(소문자 비교). 전부 포함되면 hit.
    must_contain: tuple[str, ...]


_NEEDLES: tuple[Needle, ...] = (
    Needle(
        needle_id="pct_2200f",
        statement=(
            "Per the controlling acceptance criterion, the calculated maximum fuel "
            "element cladding temperature shall not exceed 2200 degrees Fahrenheit "
            "(internal reference code QX-7731)."
        ),
        question=(
            "What is the maximum cladding temperature limit stated in the context, "
            "and what is its internal reference code? Answer with the exact number "
            "and code only."
        ),
        must_contain=("2200", "qx-7731"),
    ),
    Needle(
        needle_id="ecr_17pct",
        statement=(
            "The total oxidation of the cladding shall nowhere exceed 17 percent of "
            "the total cladding thickness before oxidation (internal reference code "
            "Z9-4410)."
        ),
        question=(
            "What is the maximum cladding oxidation percentage stated in the context, "
            "and its internal reference code? Answer with the exact number and code only."
        ),
        must_contain=("17", "z9-4410"),
    ),
)

# filler 단락 — 규제풍이되 needle 의 값/코드와 절대 겹치지 않는 잡음(distraction 모사).
# 단락마다 일련번호를 박아 동일 토큰 반복이 아니게(중복은 dedup·압축에 유리하게 작용해
# 난이도를 떨어뜨림). 길이는 budget 에 맞춰 반복.
_FILLER_UNIT = (
    "Section {n}. The applicant's design certification document describes the "
    "configuration of the auxiliary support subsystem under postulated transient "
    "conditions. Reviewers noted in the safety evaluation that the analysis method "
    "follows an accepted guidance approach, and that the staff found the demonstration "
    "adequate for the operating modes considered. No binding numerical limit is "
    "established in this paragraph; the discussion is qualitative and pertains to "
    "subsystem QA provisions, inspection intervals, and documentation completeness. "
)

# char→token 휴리스틱(runner _CHARS_PER_TOKEN 와 정렬 — 과대추정 편향). 정확 토큰화가
# 필요하면 vLLM /tokenize 를 쓸 수 있으나, 격자 라벨링엔 일관 추정으로 충분.
_CHARS_PER_TOKEN = 3


def _est_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _build_filler(target_tokens: int) -> str:
    """target_tokens 근처까지 filler 단락을 반복해 채운다(단락별 일련번호)."""
    parts: list[str] = []
    n = 0
    total = 0
    while total < target_tokens:
        unit = _FILLER_UNIT.format(n=n)
        parts.append(unit)
        total += _est_tokens(unit)
        n += 1
    return "".join(parts)


def _insert_needle(filler: str, statement: str, depth: float) -> str:
    """filler 를 단락 경계로 쪼개 depth(0..1) 위치에 needle statement 를 끼운다."""
    units = filler.split("Section ")
    units = [u for u in units if u.strip()]
    if not units:
        return statement
    idx = min(len(units), max(0, round(depth * len(units))))
    rebuilt = ["Section " + u for u in units[:idx]]
    rebuilt.append(statement + " ")
    rebuilt.extend("Section " + u for u in units[idx:])
    return "".join(rebuilt)


def _build_prompt(context: str, question: str) -> str:
    """N4 동형 골격 — [지시][CONTEXT][QUERY]. (B.3 재배치 *이전* baseline 측정용.
    배치 효과를 보려면 --layout 으로 순서를 바꿔 같은 격자를 재측정한다.)"""
    return (
        "You are a regulatory QA assistant. Answer ONLY from the CONTEXT below. "
        "Do not use prior knowledge. If the answer is not in the CONTEXT, say so.\n\n"
        "# CONTEXT\n" + context + "\n\n"
        "# QUERY\n" + question + "\n"
    )


def _build_prompt_reordered(context: str, question: str) -> str:
    """B.3 재배치안 — 질의를 CONTEXT *앞끝*에도 1회 앵커 + 지시류를 뒤끝으로."""
    return (
        "You are a regulatory QA assistant. Answer ONLY from the CONTEXT below.\n\n"
        "# QUERY (read the CONTEXT to answer this)\n" + question + "\n\n"
        "# CONTEXT\n" + context + "\n\n"
        "# QUERY\n" + question + "\n"
        "Answer ONLY from the CONTEXT. Do not use prior knowledge.\n"
    )


def _score(answer: str, needle: Needle) -> bool:
    a = answer.lower()
    return all(tok in a for tok in needle.must_contain)


@dataclass
class Result:
    needle_id: str
    budget: int
    depth: float
    trial: int
    layout: str
    hit: bool
    prompt_tokens_est: int
    latency_ms: int
    answer_excerpt: str
    error: str | None = None


def _resolve_endpoint(args) -> tuple[str, str, str | None]:
    """endpoint, model, api_key 를 args > LLM_POOL(env) 순으로 해석."""
    if args.endpoint and args.model:
        key = os.environ.get(args.api_key_env) if args.api_key_env else None
        return args.endpoint.rstrip("/"), args.model, key
    from app.config.settings import Settings  # noqa: PLC0415

    s = Settings()
    for e in s.llm_pool:
        if e.provider == "openai_compat":
            ep = args.endpoint or e.endpoint
            key = os.environ.get(e.api_key_env) if e.api_key_env else None
            return ep.rstrip("/"), args.model or e.model, key
    raise SystemExit(
        "openai_compat endpoint 를 LLM_POOL 에서 못 찾음 — --endpoint/--model 로 주세요."
    )


async def _ask(client: httpx.AsyncClient, base: str, model: str,
               api_key: str | None, prompt: str, max_tokens: int) -> tuple[str, int]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    t0 = time.monotonic()
    r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
    r.raise_for_status()
    data = r.json()
    ms = int((time.monotonic() - t0) * 1000)
    text = data["choices"][0]["message"]["content"] or ""
    return text, ms


async def main() -> int:
    ap = argparse.ArgumentParser(description="effective-context needle probe (§B.6)")
    ap.add_argument("--budgets", default="4000,16000,64000,128000,200000",
                    help="CONTEXT 토큰 예산 격자(쉼표 구분, 추정 토큰).")
    ap.add_argument("--depths", default="0.0,0.25,0.5,0.75,1.0",
                    help="needle 삽입 depth 격자(0=앞 … 1=뒤).")
    ap.add_argument("--trials", type=int, default=1, help="격자점당 반복(temp=0이라 결정론적이나 서빙 흔들림 관측용).")
    ap.add_argument("--needles", default="all", help="needle_id 쉼표 목록 또는 all.")
    ap.add_argument("--layout", choices=["baseline", "reordered", "both"],
                    default="baseline", help="프롬프트 배치(B.3 재배치안 비교).")
    ap.add_argument("--endpoint", default=None, help="OpenAI-compat base url(.../v1).")
    ap.add_argument("--model", default=None, help="served model name.")
    ap.add_argument("--api-key-env", dest="api_key_env", default=None)
    ap.add_argument("--max-tokens", type=int, default=128, help="응답 max_tokens(짧은 사실 인출).")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="동시 요청 수(단일 vLLM KV cache 경쟁 주의 — 작게).")
    ap.add_argument("--out", default=None, help="JSONL 결과 경로(미지정 시 stdout 요약만).")
    args = ap.parse_args()

    base, model, api_key = _resolve_endpoint(args)
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    depths = [float(x) for x in args.depths.split(",") if x.strip()]
    needles = _NEEDLES if args.needles == "all" else tuple(
        n for n in _NEEDLES if n.needle_id in args.needles.split(","))
    layouts = (["baseline", "reordered"] if args.layout == "both" else [args.layout])

    print(f"[exp] endpoint={base} model={model} budgets={budgets} depths={depths} "
          f"trials={args.trials} needles={[n.needle_id for n in needles]} "
          f"layouts={layouts}", file=sys.stderr)

    # 격자 펼치기 — needle 의 statement 길이를 빼고 filler 를 채워 총 예산을 맞춘다.
    jobs: list[tuple] = []
    for needle in needles:
        for budget in budgets:
            filler_budget = max(1, budget - _est_tokens(needle.statement))
            filler = _build_filler(filler_budget)
            for depth in depths:
                ctx = _insert_needle(filler, needle.statement, depth)
                for layout in layouts:
                    prompt = (_build_prompt(ctx, needle.question) if layout == "baseline"
                              else _build_prompt_reordered(ctx, needle.question))
                    p_tokens = _est_tokens(prompt)
                    for trial in range(args.trials):
                        jobs.append((needle, budget, depth, trial, layout, prompt, p_tokens))

    sem = asyncio.Semaphore(args.concurrency)
    results: list[Result] = []

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        async def run_one(job) -> None:
            needle, budget, depth, trial, layout, prompt, p_tokens = job
            async with sem:
                try:
                    answer, ms = await _ask(client, base, model, api_key,
                                            prompt, args.max_tokens)
                    res = Result(
                        needle_id=needle.needle_id, budget=budget, depth=depth,
                        trial=trial, layout=layout, hit=_score(answer, needle),
                        prompt_tokens_est=p_tokens, latency_ms=ms,
                        answer_excerpt=answer.strip()[:160],
                    )
                except Exception as exc:  # noqa: BLE001 — 실패도 1급 기록(원칙 6).
                    res = Result(
                        needle_id=needle.needle_id, budget=budget, depth=depth,
                        trial=trial, layout=layout, hit=False, prompt_tokens_est=p_tokens,
                        latency_ms=0, answer_excerpt="", error=f"{type(exc).__name__}: {exc}",
                    )
                results.append(res)
                mark = "HIT " if res.hit else ("ERR " if res.error else "miss")
                print(f"  [{mark}] {needle.needle_id} budget={budget:>7} depth={depth:<4} "
                      f"layout={layout:<9} t{trial} {res.latency_ms:>6}ms "
                      f"{res.error or res.answer_excerpt[:60]}", file=sys.stderr)

        await asyncio.gather(*(run_one(j) for j in jobs))

    # ── 요약 표: layout × budget × depth hit-rate ────────────────────────────
    print("\n=== hit-rate (layout × budget × depth) ===")
    for layout in layouts:
        print(f"\n[layout={layout}]")
        header = "budget \\ depth  " + "".join(f"{d:>8}" for d in depths)
        print(header)
        for budget in budgets:
            cells = []
            for depth in depths:
                sub = [r for r in results if r.layout == layout
                       and r.budget == budget and r.depth == depth and r.error is None]
                if sub:
                    rate = sum(1 for r in sub if r.hit) / len(sub)
                    cells.append(f"{rate:>8.2f}")
                else:
                    cells.append(f"{'—':>8}")
            print(f"{budget:>14}  " + "".join(cells))
    errs = [r for r in results if r.error]
    if errs:
        print(f"\n[errors] {len(errs)}/{len(results)} requests failed "
              f"(e.g. {errs[0].error})")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")
        print(f"\n[exp] wrote {len(results)} rows → {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
