# syntax=docker/dockerfile:1.7
#
# Frontend = OpenWebUI used as an unmodified "shell" (spec §4.1, §9 표).
# This Dockerfile only layers MVP-specific defaults + RX brand assets on top
# of the official image. Do NOT fork OpenWebUI or copy source in — if the urge
# arises, that is a signal to defer the change to Phase 2 (spec §5.2).
#
# Two stages:
#   1) `branding`  — Alpine + ImageMagick derives the 12 OpenWebUI-named
#                    static files (logo / favicons / splash / PWA icons) from
#                    the RX master PNGs in frontend/assets/. Operator drops in
#                    frontend/branding/assets/ override the auto-derived ones.
#   2) final       — Extends the official OpenWebUI image; bakes the derived
#                    static files + custom.css + a seed banner.
#
# Build:   docker build -t agent-saas/frontend:<sha> ./frontend
# Run:     see deploy/cloud/ecs/ (Fargate) and compose.onprem.yml. Secrets
#          (DATABASE_URL, WEBUI_SECRET_KEY, OPENAI_API_KEY) MUST be injected
#          at runtime, never baked in.

ARG OPENWEBUI_VERSION=v0.9.5

# =============================================================================
# Stage 1 — Derive OpenWebUI-named brand assets
# =============================================================================
FROM alpine:3.20 AS branding
RUN apk add --no-cache imagemagick

WORKDIR /work
COPY assets/RX_Logo_dark.png   /work/master-dark.png
COPY assets/RX_Logo_white.png  /work/master-white.png
COPY branding/assets/          /work/overrides/

# Derivation policy (palette §1: monochrome, white background):
#   - master-dark.png  → logo / favicons / apple-touch / PWA / splash
#   - master-white.png → favicon-dark.png only (visible on dark browser chrome)
# Aspect ratio is preserved via `-resize WxH>` (only downscale) then letterboxed
# with `-extent`. Operator overrides in branding/assets/ are applied last.
RUN set -eux; \
    mkdir -p /work/out; \
    \
    # Landscape master → logo.png (RX wordmark fits naturally landscape)
    convert master-dark.png  -resize 512x256\>   -background white -gravity center -extent 512x256   /work/out/logo.png; \
    \
    # Square favicons (transparent letterboxing keeps wordmark intact)
    convert master-dark.png  -resize 96x96\>     -background none  -gravity center -extent 96x96     /work/out/favicon.png; \
    cp /work/out/favicon.png /work/out/favicon-96x96.png; \
    convert master-white.png -resize 96x96\>     -background none  -gravity center -extent 96x96     /work/out/favicon-dark.png; \
    convert master-dark.png  -resize 180x180\>   -background none  -gravity center -extent 180x180   /work/out/apple-touch-icon.png; \
    convert master-dark.png  -resize 192x192\>   -background none  -gravity center -extent 192x192   /work/out/web-app-manifest-192x192.png; \
    convert master-dark.png  -resize 512x512\>   -background none  -gravity center -extent 512x512   /work/out/web-app-manifest-512x512.png; \
    convert master-dark.png  -define icon:auto-resize=16,32,48      /work/out/favicon.ico; \
    \
    # Splash — transparent background so OpenWebUI 의 theme(light/dark) 배경과
    # 자연스럽게 합성된다. light 는 dark 로고, dark 는 white 로고 사용.
    convert -size 1024x1024 xc:none  \( master-dark.png  -resize 768x384\> \) -gravity center -composite /work/out/splash.png; \
    convert -size 1024x1024 xc:none  \( master-white.png -resize 768x384\> \) -gravity center -composite /work/out/splash-dark.png; \
    \
    # Operator overrides win — drop properly-designed files in
    # frontend/branding/assets/ to replace any auto-derived file.
    if [ -d /work/overrides ] && [ "$(ls -A /work/overrides 2>/dev/null)" ]; then \
      find /work/overrides -maxdepth 1 -type f ! -name 'README.md' ! -name '.gitkeep' \
        -exec cp -f {} /work/out/ \; ; \
    fi; \
    \
    ls -la /work/out/

# =============================================================================
# Stage 2 — Final OpenWebUI image
# =============================================================================
FROM ghcr.io/open-webui/open-webui:${OPENWEBUI_VERSION}

# ---- Identity / locale -------------------------------------------------------
ENV WEBUI_NAME="RX AI Agent v.1.0" \
    DEFAULT_LOCALE=ko

# OpenWebUI 의 env.py 는 WEBUI_NAME 이 정확히 "Open WebUI" 가 아니면 무조건
# " (Open WebUI)" 를 덧붙인다 (의도된 attribution, env 토글 없음 — 제거 요청
# upstream PR/Discussion #3766 은 closed). fork 없이 해당 if 블록(조건+본문
# 2줄) 을 통째로 제거해 "RX AI Agent v.1.0" 만 노출되게 한다. 본문만 지우면
# 빈 if 가 남아 IndentationError 로 컨테이너가 죽으므로 두 줄을 함께 삭제.
RUN sed -i "/if WEBUI_NAME != 'Open WebUI':/,+1d" /app/backend/open_webui/env.py

# "Suggested" 프롬프트 카드 완전 제거 (아래 DEFAULT_PROMPT_SUGGESTIONS="[]" 와 짝).
# config.py 는 DEFAULT_PROMPT_SUGGESTIONS 가 빈 배열이면 "비었으니 샘플 6개로
# 채워라" 로 해석해(영어 예시 카드) 빈 값으로는 절대 비울 수 없다. 그 fallback
# 조건을 무력화해야 env 의 빈 배열이 그대로 유지된다. 프론트엔드 Suggestions
# 컴포넌트는 length==0 이면 "Suggested" 라벨+카드 영역 전체를 숨긴다.
# (조건을 'if False:' 로 치환 — 블록 삭제 대신 치환이라 들여쓰기가 안 깨진다.)
RUN sed -i "s/^if default_prompt_suggestions == \[\]:/if False:  # RX: keep empty suggestions empty/" /app/backend/open_webui/config.py

# ---- 설정 단일 소스화: env 가 항상 권위 (PersistentConfig 무력화) -----------
# OpenWebUI 의 다수 설정(USER_PERMISSIONS_*, DEFAULT_PROMPT_SUGGESTIONS, ENABLE_*
# 일부)은 "ConfigVar"(PersistentConfig) 라, 최초 부팅 시 DB(config 테이블)에
# 시드된 뒤로는 env 변경이 무시된다 (공식 문서 "Troubleshooting Ignored
# Environment Variables"). aws-mvp 는 EBS(/data)에 SQLite 를 영속화하므로 이
# 함정에 정확히 걸린다 — 재빌드해도 UI 가 안 바뀜.
#   → False 로 두면 OpenWebUI 가 DB 를 무시하고 항상 env 를 읽는다. 즉 이미지
#     /compose 가 설정의 단일 소스가 된다(MVP/IaC 정책 부합). 트레이드오프:
#     운영자가 Admin UI 로 바꾼 값은 재시작 시 사라진다(설정 드리프트 방지로 OK).
ENV ENABLE_PERSISTENT_CONFIG=false

# ---- Auth posture: invite-only, no public signup (spec §5.1, §F5) ------------
# Real WEBUI_SECRET_KEY is mounted from the secrets manager at runtime.
ENV WEBUI_AUTH=true \
    ENABLE_SIGNUP=false \
    ENABLE_LOGIN_FORM=true \
    DEFAULT_USER_ROLE=pending \
    ENABLE_API_KEY=false

# ---- Model routing: backend is the ONLY model source (spec §8.3 원칙 2) ------
# OPENAI_API_BASE_URL and OPENAI_API_KEY are set per-environment (Fargate Task
# Definition / compose.onprem.yml).
ENV ENABLE_OPENAI_API=true \
    ENABLE_OLLAMA_API=false \
    ENABLE_DIRECT_CONNECTIONS=false

# ---- Disable features outside MVP scope (spec §5.2) --------------------------
ENV ENABLE_COMMUNITY_SHARING=false \
    ENABLE_EVALUATION_ARENA_MODELS=false \
    ENABLE_RAG_WEB_SEARCH=false \
    ENABLE_WEB_SEARCH=false \
    ENABLE_IMAGE_GENERATION=false \
    ENABLE_CODE_INTERPRETER=false \
    ENABLE_CHANNELS=false \
    ENABLE_AUTOCOMPLETE_GENERATION=false \
    ENABLE_TAGS_GENERATION=false \
    ENABLE_TITLE_GENERATION=true

# ---- UI 군더더기 제거: 사용자 권한(PersistentConfig) 차원의 기능 차단 -------
# 위 ENABLE_* 는 "서버에서 기능 자체를 켜고/끄는" 글로벌 토글이고, 아래
# USER_PERMISSIONS_* 는 "사용자 역할에게 그 UI 사용권을 줄지" 권한이다 (레이어
# 다름). controls / temporary chat / notes 처럼 글로벌 토글이 없는 UI 군더더기는
# 오직 이 권한으로만 숨길 수 있다.
#
# 이 키들은 PersistentConfig(ConfigVar) 라 원래 DB 시드 후 env 가 무시되지만,
# 위 ENABLE_PERSISTENT_CONFIG=false 로 env 가 항상 권위를 갖게 했으므로 여기
# 값이 그대로 적용된다. (CHAT_CONTROLS=false 는 Controls 버튼/패널 전체를 숨기는
# master switch — 공식 문서: 이게 false 면 하위 valves/params 권한은 무의미.)
ENV USER_PERMISSIONS_CHAT_CONTROLS=false \
    USER_PERMISSIONS_CHAT_TEMPORARY=false \
    USER_PERMISSIONS_CHAT_VALVES=false \
    USER_PERMISSIONS_CHAT_SYSTEM_PROMPT=false \
    USER_PERMISSIONS_CHAT_PARAMS=false \
    USER_PERMISSIONS_FEATURES_NOTES=false \
    USER_PERMISSIONS_FEATURES_MEMORIES=false \
    USER_PERMISSIONS_FEATURES_FOLDERS=false \
    USER_PERMISSIONS_FEATURES_CALENDAR=false \
    USER_PERMISSIONS_FEATURES_CHANNELS=false \
    USER_PERMISSIONS_FEATURES_CODE_INTERPRETER=false \
    USER_PERMISSIONS_FEATURES_IMAGE_GENERATION=false \
    USER_PERMISSIONS_FEATURES_WEB_SEARCH=false

# "Suggested" 프롬프트 추천 카드 제거 (PersistentConfig, ui.prompt_suggestions).
# 빈 배열이면 신규 대화 화면의 추천 카드가 사라진다.
ENV DEFAULT_PROMPT_SUGGESTIONS="[]"

# ---- Feedback is first-class (spec §8.3 원칙 3, §F4) -------------------------
ENV ENABLE_MESSAGE_RATING=true

# ---- Admin observability (spec §F6) ------------------------------------------
ENV ENABLE_ADMIN_EXPORT=true \
    ENABLE_ADMIN_CHAT_ACCESS=true

# ---- Session / network hardening behind ALB (spec §13.1) ---------------------
# CORS_ALLOW_ORIGIN, WEBUI_URL, FORWARDED_ALLOW_IPS are env-specific → runtime.
ENV WEBUI_SESSION_COOKIE_SAME_SITE=lax \
    WEBUI_SESSION_COOKIE_SECURE=true

# ---- Data directory (managed Postgres via DATABASE_URL in prod) --------------
ENV DATA_DIR=/app/backend/data


# ---- RX brand theming (docs/meta/RX-Brand-Color-Palette.md) ------------------
# OpenWebUI ships TWO static directories and at boot copies /app/build/static/
# over /app/backend/open_webui/static/. Therefore brand overlays MUST be written
# to both, otherwise the SvelteKit-served logo wins and the backend copy is
# clobbered on container restart.
#   - /app/build/static/                  ← SvelteKit SPA bundle (browser sees this)
#   - /app/backend/open_webui/static/     ← backend-served + startup-sync target
COPY branding/custom.css /app/build/static/custom.css
COPY branding/custom.css /app/backend/open_webui/static/custom.css

# Derived brand assets from stage 1. Same dual-overlay rule as custom.css.
COPY --from=branding /work/out/ /app/build/static/
COPY --from=branding /work/out/ /app/backend/open_webui/static/

# Seed banner (PersistentConfig — admin can edit/add via Settings → Interface
# at runtime; DB-saved value overrides this default after first save). Full
# banner reference lives in frontend/branding/banners.json.
ENV WEBUI_BANNERS="[{\"id\":\"rx-internal-disclaimer\",\"type\":\"info\",\"title\":\"RX Agent — 사내 시범 운영\",\"content\":\"본 시스템은 RX Inc. 내부 도메인 전문가 대상 시범 서비스입니다. 입력하신 질의·피드백은 익명화 후 모델 개선에 활용됩니다.\",\"dismissible\":true,\"timestamp\":1715472000}]"

# Inherits from base: EXPOSE 8080, WORKDIR /app/backend, HEALTHCHECK on /health,
# CMD ["bash", "start.sh"]. Do not override.
