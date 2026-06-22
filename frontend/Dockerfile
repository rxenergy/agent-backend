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
