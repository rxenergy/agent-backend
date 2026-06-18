#!/usr/bin/env python3
"""OpenWebUI "Export All Chats" (admin DB export) JSON 에서 특정 유저의 질의 데이터를 분류·추출한다.

OpenWebUI 구조 (소스 기준, backend/open_webui/models/chats.py · routers/chats.py):

  - `/admin/settings/db` 의 "Export All Chats" = `GET /all/db` 엔드포인트
    → `list[ChatResponse]`  (전체 유저, JSON **배열**, 각 항목에 `user_id` 포함)
  - 참고: Settings > Chats 의 "Export All" = `GET /all` 는 NDJSON(단일 유저) 라 형식이 다름.

  ChatResponse 항목(최상위 키):
    id, user_id, title, chat, meta, pinned, folder_id, share_id,
    archived, created_at, updated_at, tasks, summary
  내부 `chat` blob 은 두 표현이 공존:
    chat["history"]["messages"]  → message_id 로 키된 **dict** (정본, currentId 가 리프)
    chat["messages"]             → 같은 메시지의 **list** (선형 뷰, 일부 export 에 존재)
  message 필드: id, parentId, childrenIds, role('user'|'assistant'|'system'),
    content, model/models, timestamp, files, annotation(rating/tags) ...

분류 대상: role == 'user' 인 메시지 = 사용자의 **질의(question)**.

큰 파일 전제 — 배열을 통째로 메모리에 올리지 않고 `json.JSONDecoder.raw_decode` 로
최상위 배열을 **항목 단위 스트리밍** 파싱한다(외부 의존성 없음, stdlib 만).

usage:
  # 어떤 user_id 들이 있는지 먼저 집계 (분류 대상 식별)
  python3 scripts/owui_extract_user_chats.py <export.json> --list-users

  # 특정 유저의 질의만 추출 → JSONL (메시지 1건 = 1 line)
  python3 scripts/owui_extract_user_chats.py <export.json> --user-id <UID> -o questions.jsonl

  # 세션→질의 content 배열 형태 JSON (세션 안에 user content 만 값으로)
  python3 scripts/owui_extract_user_chats.py <export.json> --user-id <UID> --by-session -o sessions.json

  # email/name 으로도 매칭 (export 에 해당 필드가 있을 때만; admin export 는 보통 user_id 만)
  python3 scripts/owui_extract_user_chats.py <export.json> --user "alice@x.com"
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator


# ──────────────────────────────────────────────────────────────────────────
# 스트리밍 파서 — 최상위 JSON 배열을 항목 단위로 흘린다(파일 통째 로드 금지).
# ──────────────────────────────────────────────────────────────────────────
def iter_top_level_array(path: Path, bufsize: int = 1 << 20) -> Iterator[Any]:
    """`[ {...}, {...}, ... ]` 형태 파일을 항목(dict) 단위로 yield.

    `raw_decode` 는 버퍼 앞쪽에서 값 하나를 디코드하고 끝 오프셋을 돌려준다.
    디코드된 만큼 버퍼에서 잘라내고, 항목 사이의 공백/쉼표는 건너뛴다.
    객체 하나가 버퍼보다 크면 더 읽어 이어 붙인다(부분 디코드 실패 → refill).
    """
    decoder = json.JSONDecoder()
    buf = ""
    started = False  # 최상위 '[' 를 지났는가
    with path.open("r", encoding="utf-8") as fh:
        while True:
            if not started:
                # 첫 비공백 문자가 '[' 인지 확인. 객체 하나(NDJSON 아님)면 안내.
                while True:
                    buf = buf.lstrip()
                    if buf:
                        break
                    chunk = fh.read(bufsize)
                    if not chunk:
                        return
                    buf += chunk
                if buf[0] == "{":
                    raise ValueError(
                        "최상위가 배열이 아니라 객체입니다. 이 스크립트는 admin "
                        "'Export All Chats'(JSON 배열)용입니다. NDJSON(/all) 이면 "
                        "--ndjson 으로 다시 실행하세요."
                    )
                if buf[0] != "[":
                    raise ValueError(f"예상치 못한 최상위 토큰: {buf[0]!r}")
                buf = buf[1:]
                started = True

            # 다음 항목 디코드 시도
            buf = buf.lstrip()
            if buf[:1] == ",":
                buf = buf[1:].lstrip()
            if buf[:1] == "]":
                return
            if not buf:
                chunk = fh.read(bufsize)
                if not chunk:
                    return
                buf += chunk
                continue
            try:
                obj, end = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                # 버퍼에 항목이 덜 들어옴 → 더 읽어서 이어 붙이고 재시도.
                chunk = fh.read(bufsize)
                if not chunk:
                    # 더 읽을 게 없는데 디코드 실패 = 잘린 파일.
                    raise
                buf += chunk
                continue
            yield obj
            buf = buf[end:]


def iter_ndjson(path: Path) -> Iterator[Any]:
    """`/all` (Settings > Chats) NDJSON 폴백 — 한 줄 = 한 chat."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


# ──────────────────────────────────────────────────────────────────────────
# chat blob → 메시지 평탄화
# ──────────────────────────────────────────────────────────────────────────
def iter_messages(chat_blob: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """chat blob 에서 메시지를 시간순으로 흘린다.

    정본은 history.messages(dict). currentId 에서 parentId 로 역추적하면 '활성 분기'
    만 얻지만, 분류 목적상 **모든 분기의 user 메시지**를 원하므로 dict 전체를
    timestamp 순으로 정렬해 내보낸다. history 가 없으면 선형 `messages` list 폴백.
    """
    history = chat_blob.get("history") if isinstance(chat_blob, dict) else None
    if isinstance(history, dict) and isinstance(history.get("messages"), dict):
        msgs = list(history["messages"].values())
        msgs.sort(key=lambda m: m.get("timestamp") or 0)
        yield from msgs
        return
    # 폴백: 선형 list
    linear = chat_blob.get("messages") if isinstance(chat_blob, dict) else None
    if isinstance(linear, list):
        yield from linear


def message_content_text(msg: dict[str, Any]) -> str:
    """content 가 문자열이거나 멀티모달 파트 리스트일 수 있다 → 텍스트만 합친다."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict) and p.get("type") in (None, "text"):
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(x for x in parts if x)
    return "" if c is None else str(c)


# ──────────────────────────────────────────────────────────────────────────
# 유저 매칭
# ──────────────────────────────────────────────────────────────────────────
def chat_matches_user(chat: dict[str, Any], *, user_id: str | None, user: str | None) -> bool:
    if user_id is not None:
        return chat.get("user_id") == user_id
    if user is not None:
        # admin export 는 보통 user_id 만 담지만, 일부 가공본은 email/name 을 붙임.
        for key in ("user_id", "email", "user_email", "name", "user_name"):
            v = chat.get(key)
            if isinstance(v, str) and v == user:
                return True
        # meta 안에 들어있는 경우도 방어적으로 본다.
        meta = chat.get("meta")
        if isinstance(meta, dict):
            for v in meta.values():
                if v == user:
                    return True
        return False
    return True  # 필터 없음 = 전부


# ──────────────────────────────────────────────────────────────────────────
# 모드: --list-users
# ──────────────────────────────────────────────────────────────────────────
def run_list_users(chats: Iterator[dict[str, Any]]) -> None:
    by_user: Counter[str] = Counter()
    q_by_user: Counter[str] = Counter()
    total = 0
    for chat in chats:
        total += 1
        uid = chat.get("user_id") or "<no user_id>"
        by_user[uid] += 1
        for m in iter_messages(chat.get("chat") or {}):
            if m.get("role") == "user":
                q_by_user[uid] += 1
    print(f"# 총 chat(세션) 수: {total}, 유저(user_id) 수: {len(by_user)}", file=sys.stderr)
    print(f"{'user_id':<40} {'sessions':>9} {'questions':>10}")
    for uid, n in by_user.most_common():
        print(f"{uid:<40} {n:>9} {q_by_user.get(uid, 0):>10}")


# ──────────────────────────────────────────────────────────────────────────
# 모드: 추출
# ──────────────────────────────────────────────────────────────────────────
def run_extract(
    chats: Iterator[dict[str, Any]],
    *,
    user_id: str | None,
    user: str | None,
    out,
    include_assistant: bool,
) -> tuple[int, int]:
    n_sessions = 0
    n_questions = 0
    for chat in chats:
        if not chat_matches_user(chat, user_id=user_id, user=user):
            continue
        n_sessions += 1
        session_id = chat.get("id")
        title = chat.get("title")
        created_at = chat.get("created_at")
        for m in iter_messages(chat.get("chat") or {}):
            role = m.get("role")
            if role == "user" or (include_assistant and role == "assistant"):
                if role == "user":
                    n_questions += 1
                rec = {
                    "user_id": chat.get("user_id"),
                    "session_id": session_id,
                    "session_title": title,
                    "session_created_at": created_at,
                    "message_id": m.get("id"),
                    "parent_id": m.get("parentId"),
                    "role": role,
                    "content": message_content_text(m),
                    "model": m.get("model") or m.get("models"),
                    "timestamp": m.get("timestamp"),
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return n_sessions, n_questions


def run_extract_by_session(
    chats: Iterator[dict[str, Any]],
    *,
    user_id: str | None,
    user: str | None,
    out,
    include_assistant: bool,
) -> tuple[int, int]:
    """세션 → content 배열 형태의 단일 JSON 객체를 만든다.

    { "<session_id>": ["질의1", "질의2", ...], ... }
    값은 user(옵션에 따라 assistant 포함) 메시지의 content 텍스트만. 메타데이터 없음.
    세션 id 가 비면 안정적 폴백 키(session-<index>)를 쓴다. 매칭된 유저의 세션만
    메모리에 올린다(전체 export 가 아니라 유저 1명분이라 안전).
    """
    result: dict[str, list[str]] = {}
    n_questions = 0
    idx = 0
    for chat in chats:
        if not chat_matches_user(chat, user_id=user_id, user=user):
            continue
        sid = chat.get("id") or f"session-{idx}"
        idx += 1
        contents = result.setdefault(sid, [])
        for m in iter_messages(chat.get("chat") or {}):
            role = m.get("role")
            if role == "user" or (include_assistant and role == "assistant"):
                if role == "user":
                    n_questions += 1
                contents.append(message_content_text(m))
    json.dump(result, out, ensure_ascii=False, indent=2)
    out.write("\n")
    return len(result), n_questions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path, help="OpenWebUI admin 'Export All Chats' JSON 경로")
    ap.add_argument("--list-users", action="store_true", help="유저별 세션/질의 수 집계만 출력하고 종료")
    ap.add_argument("--user-id", help="추출할 user_id (정확 일치). admin export 의 정본 식별자.")
    ap.add_argument("--user", help="email/name 으로 매칭(가공본 전용; admin export 엔 보통 없음)")
    ap.add_argument("-o", "--out", type=Path, help="출력 JSONL 경로 (미지정 시 stdout)")
    ap.add_argument("--include-assistant", action="store_true", help="응답(assistant)도 함께 출력")
    ap.add_argument(
        "--by-session",
        action="store_true",
        help="세션→content 배열 형태의 단일 JSON 으로 출력(세션 안에 content 만 값으로)",
    )
    ap.add_argument("--ndjson", action="store_true", help="입력이 NDJSON(/all 형식)일 때")
    args = ap.parse_args(argv)

    if not args.export.exists():
        print(f"입력 파일 없음: {args.export}", file=sys.stderr)
        return 2

    reader = iter_ndjson if args.ndjson else iter_top_level_array

    if args.list_users:
        run_list_users(reader(args.export))
        return 0

    if not args.user_id and not args.user:
        print("추출하려면 --user-id 또는 --user 가 필요합니다. (먼저 --list-users 로 확인)", file=sys.stderr)
        return 2

    out = args.out.open("w", encoding="utf-8") if args.out else sys.stdout
    try:
        extract = run_extract_by_session if args.by_session else run_extract
        n_sessions, n_questions = extract(
            reader(args.export),
            user_id=args.user_id,
            user=args.user,
            out=out,
            include_assistant=args.include_assistant,
        )
    finally:
        if args.out:
            out.close()

    target = args.user_id or args.user
    print(
        f"# 유저 {target!r}: 세션 {n_sessions}개, 질의 {n_questions}건 추출"
        + (f" → {args.out}" if args.out else ""),
        file=sys.stderr,
    )
    if n_sessions == 0:
        print("# 매칭 0건 — --list-users 로 실제 user_id 를 확인하세요.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
