# RX Agent — Brand Asset Overrides (Optional)

이 디렉토리는 **선택사항**이다. 비어 있어도 Dockerfile 빌드는 정상 진행된다.

## 자동 파생 (기본 동작)

`frontend/Dockerfile` 의 `branding` 스테이지가 `frontend/assets/RX_Logo_dark.png` 과 `frontend/assets/RX_Logo_white.png` 마스터로부터 OpenWebUI 가 요구하는 12개의 정적 파일을 ImageMagick 으로 자동 생성한다. 따로 작업하지 않으면 그 결과가 그대로 이미지에 들어간다.

| 파생 파일 | 마스터 | 처리 |
|----------|--------|-----|
| `logo.png` | `RX_Logo_dark.png` | 512×256 white-letterbox |
| `favicon.png`, `favicon-96x96.png` | `RX_Logo_dark.png` | 96×96 transparent |
| `favicon-dark.png` | `RX_Logo_white.png` | 96×96 transparent (다크 UI chrome 용) |
| `apple-touch-icon.png` | `RX_Logo_dark.png` | 180×180 transparent |
| `web-app-manifest-192x192.png` | `RX_Logo_dark.png` | 192×192 transparent |
| `web-app-manifest-512x512.png` | `RX_Logo_dark.png` | 512×512 transparent |
| `favicon.ico` | `RX_Logo_dark.png` | 16/32/48 multi-resolution |
| `splash.png`, `splash-dark.png` | `RX_Logo_dark.png` | 1024×1024 white background |

## Override 슬롯 (필요할 때만)

자동 파생이 만족스럽지 않을 때 — 예: 정사각 favicon 에 가로 워드마크 letterboxing 이 보기 안 좋고 별도 디자인된 정사각 아이콘이 있을 때 — 다음 파일들 중 원하는 것만 이 디렉토리에 드롭하면 빌드 시 자동 파생본을 **그 파일이 덮어쓴다**.

| 파일명 | 형식·크기 | 비고 |
|--------|----------|-----|
| `logo.png` | PNG, 권장 512×256 | 사이드바·로그인 메인 |
| `favicon.png` | PNG, 96×96 | |
| `favicon-96x96.png` | PNG, 96×96 | favicon.png 와 동일 가능 |
| `favicon.svg` | SVG, viewBox 32×32 | **자동 파생되지 않음** — 필요시 직접 공급 |
| `favicon.ico` | ICO, multi-res | |
| `favicon-dark.png` | PNG, 96×96 | 다크 UI chrome 용 |
| `apple-touch-icon.png` | PNG, 180×180 | iOS 홈 화면 |
| `splash.png` | PNG, 1024×1024 | 흰 배경 위 로고 중앙 |
| `splash-dark.png` | PNG, 1024×1024 | RX 정책상 보통 splash.png 와 동일 |
| `web-app-manifest-192x192.png` | PNG, 192×192 | PWA |
| `web-app-manifest-512x512.png` | PNG, 512×512 | PWA |

이 README.md 와 `.gitkeep` 은 `.dockerignore` 와 빌드 스테이지가 자동으로 제외하므로 그대로 두면 된다.

## 디자인 규칙

`docs/meta/RX-Brand-Color-Palette.md` 와 동일.
- 채도 있는 색 0건. RX neutral (`#1F1F1F` ~ `#FFFFFF`) 만 사용.
- 그라디언트 금지.
- 컬러 사진·일러스트는 grayscale 변환 필수.

## 검증

```bash
docker build -t agent-saas/frontend:dev ./frontend
docker run --rm agent-saas/frontend:dev ls /app/backend/open_webui/static/ \
  | grep -E '(logo|favicon|splash|apple-touch|web-app-manifest)'
```
파일 11~12개가 출력되면 정상 (favicon.svg 는 공급한 경우에만).
