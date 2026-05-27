# RX Agent — Frontend Branding

`frontend/Dockerfile` 이 빌드 시 두 가지 입력을 합쳐 OpenWebUI 이미지에 굽는다:

1. **CSS 테마** — `custom.css` (`docs/meta/RX-Brand-Color-Palette.md` 의 코드화)
2. **정적 자산** — `frontend/assets/RX_Logo_{dark,white}.png` 마스터 → Dockerfile 의 `branding` 스테이지(Alpine + ImageMagick)가 OpenWebUI 가 요구하는 12개의 정사각·정확한-크기 파일로 자동 파생. `frontend/branding/assets/` 에 운영자가 동일 파일명으로 드롭하면 자동 파생본을 override.

또한 `banners.json` 은 시스템 배너 콘텐츠의 canonical reference. 그 중 핵심 1건은 `Dockerfile` 의 `ENV WEBUI_BANNERS` 로 baked-in 되어 첫 부팅부터 노출되고, 나머지는 운영자가 OpenWebUI 의 `Settings → Interface → Banners` 에서 추가한다.

## 파일 구성

```
frontend/
├── assets/                               ← 마스터 (디자인 원본)
│   ├── RX_Logo_dark.png                  ← 다크 워드마크 (밝은 배경용)
│   └── RX_Logo_white.png                 ← 화이트 워드마크 (어두운 배경용)
│
├── Dockerfile                            ← branding 스테이지가 위 마스터를 12개 파일로 파생
│
└── branding/
    ├── README.md                         ← 이 파일
    ├── custom.css                        ← /app/backend/open_webui/static/custom.css 로 COPY
    ├── banners.json                      ← Settings → Interface 에 붙여 넣을 배너 reference
    └── assets/                           ← override 슬롯 (선택)
        ├── README.md                     ← 자동 파생 표 + override 가이드
        └── .gitkeep                      ← 빈 디렉토리 유지용
```

## 운영 절차

### A. 첫 빌드

1. 마스터 워드마크 두 장이 `frontend/assets/RX_Logo_{dark,white}.png` 에 있는지 확인 (현재 4226×2183, 투명 RGBA — 그대로 사용 가능).
2. `docker build -t agent-saas/frontend:dev ./frontend`
3. 로컬 검증:
   ```bash
   docker run --rm -p 8080:8080 \
     -e WEBUI_AUTH=false \
     -e OPENAI_API_BASE_URL=https://example.com \
     -e OPENAI_API_KEY=sk-fake \
     agent-saas/frontend:dev
   ```
   브라우저에서 `http://localhost:8080` 접속 → 로고·favicon·테마 적용 확인. WEBUI_AUTH=false 는 로컬 검증 한정 — 프로덕션에선 절대 사용 금지.
4. `docs/meta/RX-Brand-Color-Palette.md §8 Pre-Publish Checklist` 9개 항목 모두 통과하는지 시각적으로 확인.
5. ECR push → ECS Service rolling deploy (`deploy/cloud/README.md §2.3`).

### B. CSS 만 빠르게 수정 — 재빌드 없이

OpenWebUI 어드민 권한 사용자는 다음 경로로 즉시 CSS 를 덮어쓸 수 있다.

> **Settings (⚙️) → Interface → Custom CSS**

`custom.css` 의 내용을 그대로 붙여넣고 저장. PersistentConfig 가 DB 에 저장되어 컨테이너 재시작 후에도 유지된다. **단, 이 변경은 이미지 밖에 있어 IaC·CI 추적이 안 되므로 정식 변경은 반드시 `custom.css` 를 수정하고 이미지를 재빌드해서 ECR push 하는 경로를 사용.**

긴급 패치(시연 30분 전 색이 잘못 보임 등)에만 UI 경로 허용. 빠르게 고친 뒤 같은 변경을 파일에 반영하고 새 이미지로 deploy → DB 의 임시 override 를 삭제.

### C. 배너 수정

`banners.json` 의 항목을 OpenWebUI 어드민의 `Settings → Interface → Banners` 에서 추가/수정. 첫 부팅의 default 한 건은 `Dockerfile` ENV 로 들어가 있으므로, 빈 DB 상태(첫 배포)에서도 사용자에게 disclaimer 가 즉시 노출된다.

배너 ID(`rx-internal-disclaimer`, `rx-data-handling` 등)는 변경하지 말 것 — 동일 ID 를 가진 사용자별 dismiss 기록이 새 배너로 잘못 적용된다.

### D. 자산 교체

**마스터 워드마크가 바뀐 경우** (회사 로고 리뉴얼 등):
1. `frontend/assets/RX_Logo_{dark,white}.png` 를 새 PNG 로 교체 (해상도·종횡비는 유연 — branding 스테이지가 알아서 리사이즈).
2. `docker build` → push → deploy. 12개 파생 파일이 새 마스터로부터 다시 생성된다.

**특정 파일 하나만 수동 디자인하고 싶을 때** (예: 정사각 아이콘 마크가 따로 있음):
1. 해당 파일을 OpenWebUI 가 요구하는 정확한 파일명으로 `frontend/branding/assets/` 에 드롭. 파일명 목록은 `branding/assets/README.md` 참조.
2. 빌드 시 branding 스테이지가 자동 파생본을 만들고, 그 위에 운영자 override 가 덮어쓴다.
3. 빌드·push·deploy.

## 디자인 원칙 (요약)

`docs/meta/RX-Brand-Color-Palette.md` 와 동일.

- 채도 있는 색 **0건** — 파랑·빨강·녹색·노랑·주황·보라 등 사용 금지.
- 그라디언트 fill **금지** — 단색 채움 또는 단색 라인만.
- 흰색 배경 고정 — 다크 모드는 CSS 로 라이트 강제.
- 폰트 — Pretendard → Noto Sans KR → Malgun Gothic → Arial.
- 시맨틱 색(`successInk`/`warnInk`/`dangerInk`)은 텍스트·작은 아이콘에만, 큰 면적 fill 금지.

이 원칙을 어기는 자산이 들어오면 빌드가 실패하지는 않지만 **사전 발행 체크리스트가 통과하지 못한다.** 코드 리뷰 단계에서 거른다.

## 디버깅

### CSS 가 적용되지 않을 때

1. 브라우저 캐시 강제 새로고침 (Cmd/Ctrl + Shift + R).
2. 컨테이너 진입 후 파일 존재 확인:
   ```bash
   docker exec -it <container> ls -la /app/backend/open_webui/static/custom.css
   ```
3. 브라우저 DevTools → Network 탭에서 `custom.css` 요청이 200 OK 인지 확인.
4. 그래도 안 되면: 위 **B. CSS 만 빠르게 수정** 경로로 Settings → Custom CSS 에 paste 해서 우회. 동시에 OpenWebUI 버전이 정적 파일 로딩 방식을 바꿨는지 changelog 확인.

### 다크 모드가 살아남을 때

`custom.css` §2 의 `html.dark` override 가 OpenWebUI 의 새 다크 셀렉터를 못 따라잡았을 가능성. 브라우저 DevTools 로 다크 상태의 root 클래스명을 확인하고 §2 에 추가.

### 폰트가 Pretendard 가 아닐 때

내부망에서 `cdn.jsdelivr.net` 차단됐을 가능성. `custom.css §0` 의 @import 를 주석 처리하고 Pretendard webfont 파일을 `assets/fonts/` 에 직접 번들 → `@font-face` 로 로컬 참조하도록 §0 를 수정.

## 변경 이력

| 일자 | 변경 | 이유 |
|------|------|------|
| 2026-05-12 | 초기 작성 | RX 모노크롬 브랜드 적용 |
| 2026-05-26 | `custom.css` 안전판 재작성 | OpenWebUI 컴포넌트 깨짐(`header`/`[class*="..."]`/Tailwind sweep 의 broad selector + `!important` 조합) 제거. 토큰 정의 + body 폰트 + selection 만 유지. 채색 sweep·컴포넌트 오버라이드 제거. 로고/파비콘은 Dockerfile 정적 자산 경로로 별도 적용되므로 CSS 비활성화돼도 RX 브랜딩 최저선 유지. |
