# RX Brand Color Palette

**RX Inc. Visual Identity Guidelines — Color System**

> Version 1.1 · 2026-05-18

### Changelog

- **v1.1 (2026-05-18)** — 다크 모드 ramp 정식 도입 (§3-Dark, §7-Dark). 디지털 산출물 한정으로 흰색 배경 강제 규칙을 완화: 정적 문서(PPT·PDF·인쇄)는 light 만 사용, 동적 UI(웹 앱)는 light/dark 양 변종 허용. §1 절대 규칙 #1 (채도 0) / #2 (그라디언트 0) 는 두 모드 공통.
- **v1.0 (2026-05-12)** — 초안. 라이트 모노크롬 ramp 만 정의.

---

## 1. Brand Color Philosophy

RX 브랜드의 시각 정체성은 **무채색 모노크롬**으로 정의된다. 모든 발표 자료·문서·다이어그램·디지털 산출물은 흰색 배경 위 그레이 스케일만 사용한다.

### 절대 규칙 (모든 산출물 공통)

- 채도가 있는 색(파랑·빨강·녹색·노랑·보라·주황 등) **사용 금지**.
- 그라디언트(gradient) fill **사용 금지**.
- 컬러 사진·일러스트가 필요한 경우에도 **흑백(grayscale) 변환** 후 삽입.
- 시맨틱 상태색(완료·경고·차단)은 §5에 정의된 톤에 한해 **텍스트·작은 아이콘**으로만 사용. 도형 채움·차트 시리즈·큰 면적 fill에는 금지.

### 산출물 유형별 배경 규칙

| 산출물 유형 | 허용 배경 | 비고 |
|-------------|-----------|------|
| **정적 문서** — PPT·PDF·인쇄물·다이어그램·차트 이미지 | 흰색(`#FFFFFF`) 단일 | 인쇄 / 슬라이드 / 사진 출력 일관성 보장 |
| **동적 UI** — 웹 앱·관리 콘솔·내부 도구 | light(흰색) 또는 dark(ink) 양 변종 허용 | 사용자 OS / 사용자 명시 토글 따름. 두 변종 모두 §3·§3-Dark ramp 안에서만 구성 |

채도 0·그라디언트 0 원칙은 어느 변종에서도 흔들리지 않는다. 다크 변종은 *팔레트의 반전*이지 *색 추가*가 아니다.

---

## 2. Core Palette

브랜드 정체성의 기반이 되는 3색. 표지·헤더·본문·로고 등 거의 모든 시각 요소가 이 3색의 조합으로 성립한다.

| Name | HEX | RGB | 사용처 |
|------|-----|-----|--------|
| **Primary Dark** | `#535353` | 83, 83, 83 | 본문 텍스트, 헤더 텍스트, 불릿, 구분선, 로고 본색 |
| **Header Bar** | `#3B3838` | 59, 56, 56 | 콘텐츠 슬라이드 상단 다크 헤더 바, 강조 영역 |
| **Pure White** | `#FFFFFF` | 255, 255, 255 | 슬라이드/문서 배경, 다크 헤더 위 텍스트 |

### Visual

```
┌──────────────┬──────────────┬──────────────┐
│              │              │              │
│   #535353    │   #3B3838    │   #FFFFFF    │
│   Primary    │   Header     │   White      │
│              │              │              │
└──────────────┴──────────────┴──────────────┘
```

---

## 3. Extended Neutral Ramp (9-Step Grayscale)

텍스트 위계·표·도형·배경 깊이를 위한 9단계 그레이 램프. 인접 토큰 간 명도 차가 일정해 시각적 깊이를 안정적으로 제공한다.

| Token | HEX | Lightness | Recommended Use |
|-------|-----|-----------|-----------------|
| `ink` | `#1F1F1F` | 12% | 최상위 강조 텍스트, 핵심 지표 수치 |
| `headerBar` | `#3B3838` | 23% | 다크 헤더 바, 차트 주력 시리즈 |
| `primary` | `#535353` | 33% | 본문 기본색, 로고 |
| `mutedText` | `#8C8C8C` | 55% | 캡션, 라벨, 보조 설명 |
| `border` | `#BFBFBF` | 75% | 표 테두리, 도형 외곽선 |
| `divider` | `#D9D9D9` | 85% | 구분선, 얇은 분리 라인 |
| `subtle` | `#EDEDED` | 93% | 표 헤더 배경, 약한 강조 박스 |
| `surface` | `#F7F7F7` | 97% | 카드 배경, 정보 박스 배경 |
| `white` | `#FFFFFF` | 100% | 문서·슬라이드 본 배경 |

### Adjacency Rule

인접하여 배치하는 토큰끼리는 **명도 차 30% 이상**을 유지한다. 예를 들어 `primary`(33%) 위에는 `subtle`(93%) 또는 `surface`(97%)를 사용하고, `mutedText`(55%) 위에 `border`(75%)를 겹치는 식의 저대비 조합은 피한다.

### Visual Ramp

```
ink ──────── headerBar ──── primary ──── mutedText ──── border ──── divider ──── subtle ──── surface ──── white
#1F1F1F      #3B3838         #535353       #8C8C8C       #BFBFBF      #D9D9D9      #EDEDED      #F7F7F7      #FFFFFF
```

---

## 3-Dark. Dark Mode Ramp (Digital UI Only)

동적 UI 변종(라이트 ⇄ 다크) 중 다크 모드에서 사용하는 9단계 ramp. 시맨틱은 §3과 동일하며, *값만* 반전한다. 토큰 의미는 두 모드에서 같으므로 컴포넌트 정의는 같은 토큰을 참조하면 자동으로 모드별 색이 적용된다.

| Token | HEX (Light) | HEX (Dark) | Semantic Use |
|-------|-------------|-----------|--------------|
| `white` (page bg) | `#FFFFFF` | `#1F1F1F` | 페이지 본 배경 |
| `surface` | `#F7F7F7` | `#2A2A2A` | 카드 / 사이드바 배경 |
| `subtle` | `#EDEDED` | `#333333` | 약한 강조 박스, 표 헤더, 사용자 메시지 버블 |
| `divider` | `#D9D9D9` | `#404040` | 얇은 구분선 |
| `border` | `#BFBFBF` | `#595959` | 입력 외곽선, 강한 구분선 |
| `mutedText` | `#8C8C8C` | `#A6A6A6` | 캡션·placeholder (다크는 AAA 확보 위해 톤업) |
| `primary` | `#535353` | `#D9D9D9` | 본문 텍스트 |
| `ink` | `#1F1F1F` | `#FFFFFF` | 강조 헤드라인, 핵심 지표 |
| `headerBar` | `#3B3838` | `#4A4A4A` | 다크 헤더 바, 차트 주력 시리즈 (다크에선 배경 대비 유지 위해 톤업) |

### Fill Helper Tokens (양 모드 공통 시맨틱)

primary 액션 버튼 / `bg-blue-500` 등 "주력 fill + 그 위 글자" 쌍은 모드 무관 한 쌍으로 사용한다. 라이트에선 어두운 fill + 흰 글자, 다크에선 밝은 fill + 어두운 글자로 자동 반전된다.

| Token | Light | Dark | Use |
|-------|-------|------|-----|
| `fillStrong` | `#3B3838` | `#E5E5E5` | 주력 fill (CTA 버튼, ::selection 등) |
| `onFillStrong` | `#FFFFFF` | `#1F1F1F` | `fillStrong` 위의 글자 색 |
| `pureWhite` | `#FFFFFF` | `#FFFFFF` | Tailwind `.text-white` 처럼 *항상* 흰색을 의도하는 anchor |

### Dark Visual Ramp

```
white(bg) ──── surface ──── subtle ──── divider ──── border ──── mutedText ──── primary ──── ink
#1F1F1F        #2A2A2A      #333333      #404040      #595959      #A6A6A6        #D9D9D9      #FFFFFF
```

### Adjacency Rule (Dark)

라이트와 동일하게 인접 토큰 간 **명도 차 30% 이상** 유지. 다크 모드에서도 `border`(35%) 위에 `mutedText`(65%) 같은 저대비 조합은 금지.

---

## 4. Chart Series Palette

데이터 시각화(막대·선·도넛 차트)에 사용하는 5단계 그레이 시리즈. 시리즈 1이 가장 진한 강조, 시리즈 5가 가장 옅은 배경 데이터를 표현한다.

| Series | HEX | Use |
|--------|-----|-----|
| Series 1 | `#3B3838` | 주력 데이터 (가장 진함) |
| Series 2 | `#535353` | 보조 데이터 |
| Series 3 | `#8C8C8C` | 비교군 |
| Series 4 | `#BFBFBF` | 배경 데이터 |
| Series 5 | `#D9D9D9` | 최하위 시리즈 (가장 옅음) |

### Visual

```
■■■■■  Series 1 — #3B3838
■■■■   Series 2 — #535353
■■■    Series 3 — #8C8C8C
■■     Series 4 — #BFBFBF
■      Series 5 — #D9D9D9
```

> 다수의 그래프 도구(PowerPoint·pptxgenjs·Excel 등)는 기본 컬러로 파랑·오렌지 시리즈를 자동 주입하므로, 차트를 생성할 때 위 5색을 **명시적으로** 지정해야 한다.

---

## 5. Semantic Accent Inks (Restricted Use)

상태 표시를 위한 3가지 약채도 톤. 채도를 30% 이하로 낮춰 그레이 팔레트와 충돌하지 않게 조정된 색이다. 동적 UI 의 다크 변종에서는 배경 대비를 위해 톤업한 값이 별도 매핑된다.

| Token | HEX (Light) | HEX (Dark) | Status |
|-------|-------------|-----------|--------|
| `successInk` | `#2D5A3D` | `#8FBF9F` | 완료 · 정상 |
| `warnInk` | `#7A5A1F` | `#D9B97A` | 검토 필요 · 지연 |
| `dangerInk` | `#7A2D2D` | `#D98F8F` | 차단 · 블로커 |

### Allowed

- 상태 라벨 텍스트 (예: "완료", "지연")
- 작은 아이콘·체크마크
- 표 셀 내 상태 텍스트
- 알림 박스의 텍스트 색

### Forbidden

- 슬라이드·문서 배경
- 헤더 바
- 도형 채움(fill)
- 차트 시리즈
- 큰 면적의 fill

큰 면적에 사용하면 모노크롬 정체성을 깨뜨리므로, 시맨틱 색은 항상 **선·점 수준의 면적**에서만 등장한다.

---

## 6. Typography Pairing

폰트 자체는 컬러가 아니지만, 팔레트와 함께 사용할 때 기준이 되는 조합을 명시한다.

- **Primary**: Pretendard
- **Bold weights**: Pretendard SemiBold, Pretendard Black
- **Medium**: Pretendard Medium
- **Fallback**: Noto Sans KR → Malgun Gothic → Arial

본문은 `primary`(`#535353`), 강조 헤드라인은 `ink`(`#1F1F1F`), 보조 설명은 `mutedText`(`#8C8C8C`)를 원칙으로 한다.

---

## 7. Implementation Tokens

브랜드 산출물을 자동화 도구로 생성할 때 사용할 수 있는 토큰 객체 형식.

### JSON

```json
{
  "core": {
    "primary":   "#535353",
    "headerBar": "#3B3838",
    "white":     "#FFFFFF"
  },
  "ramp": {
    "light": {
      "white":     "#FFFFFF",
      "surface":   "#F7F7F7",
      "subtle":    "#EDEDED",
      "divider":   "#D9D9D9",
      "border":    "#BFBFBF",
      "mutedText": "#8C8C8C",
      "primary":   "#535353",
      "ink":       "#1F1F1F",
      "headerBar": "#3B3838"
    },
    "dark": {
      "white":     "#1F1F1F",
      "surface":   "#2A2A2A",
      "subtle":    "#333333",
      "divider":   "#404040",
      "border":    "#595959",
      "mutedText": "#A6A6A6",
      "primary":   "#D9D9D9",
      "ink":       "#FFFFFF",
      "headerBar": "#4A4A4A"
    }
  },
  "fillHelpers": {
    "light": { "fillStrong": "#3B3838", "onFillStrong": "#FFFFFF" },
    "dark":  { "fillStrong": "#E5E5E5", "onFillStrong": "#1F1F1F" },
    "pureWhite": "#FFFFFF"
  },
  "chartSeries": [
    "#3B3838", "#535353", "#8C8C8C", "#BFBFBF", "#D9D9D9"
  ],
  "semanticInk": {
    "light": { "success": "#2D5A3D", "warn": "#7A5A1F", "danger": "#7A2D2D" },
    "dark":  { "success": "#8FBF9F", "warn": "#D9B97A", "danger": "#D98F8F" }
  }
}
```

### CSS Custom Properties

토큰 *이름* 은 시맨틱이라 light/dark 공통이고, *값* 만 모드별로 swap 한다. `:root` 에 라이트 값을 두고 `html.dark` / `[data-theme="dark"]` / `.dark` 범위에서 다크 값으로 재정의한다. `prefers-color-scheme` 미디어 쿼리는 OS 선호 폴백용.

```css
/* Light theme (default) */
:root {
  --rx-white:      #FFFFFF;
  --rx-surface:    #F7F7F7;
  --rx-subtle:     #EDEDED;
  --rx-divider:    #D9D9D9;
  --rx-border:     #BFBFBF;
  --rx-muted:      #8C8C8C;
  --rx-primary:    #535353;
  --rx-ink:        #1F1F1F;
  --rx-header-bar: #3B3838;

  /* Semantic ink (text only) */
  --rx-success:    #2D5A3D;
  --rx-warn:       #7A5A1F;
  --rx-danger:     #7A2D2D;

  /* Fill helpers */
  --rx-fill-strong:    #3B3838;
  --rx-on-fill-strong: #FFFFFF;
  --rx-pure-white:     #FFFFFF;

  color-scheme: light;
}

/* Dark theme — UI 토글 또는 OS prefers-color-scheme */
html.dark,
[data-theme="dark"],
.dark {
  --rx-white:      #1F1F1F;
  --rx-surface:    #2A2A2A;
  --rx-subtle:     #333333;
  --rx-divider:    #404040;
  --rx-border:     #595959;
  --rx-muted:      #A6A6A6;
  --rx-primary:    #D9D9D9;
  --rx-ink:        #FFFFFF;
  --rx-header-bar: #4A4A4A;

  --rx-success:    #8FBF9F;
  --rx-warn:       #D9B97A;
  --rx-danger:     #D98F8F;

  --rx-fill-strong:    #E5E5E5;
  --rx-on-fill-strong: #1F1F1F;
  /* --rx-pure-white anchor — 다크에서도 #FFFFFF 유지 */

  color-scheme: dark;
}
```

### JavaScript

```javascript
const RX_PALETTE = {
  core: {
    primary:   '#535353',
    headerBar: '#3B3838',
    white:     '#FFFFFF',
  },
  ramp: {
    light: {
      white:     '#FFFFFF',
      surface:   '#F7F7F7',
      subtle:    '#EDEDED',
      divider:   '#D9D9D9',
      border:    '#BFBFBF',
      mutedText: '#8C8C8C',
      primary:   '#535353',
      ink:       '#1F1F1F',
      headerBar: '#3B3838',
    },
    dark: {
      white:     '#1F1F1F',
      surface:   '#2A2A2A',
      subtle:    '#333333',
      divider:   '#404040',
      border:    '#595959',
      mutedText: '#A6A6A6',
      primary:   '#D9D9D9',
      ink:       '#FFFFFF',
      headerBar: '#4A4A4A',
    },
  },
  fillHelpers: {
    light:     { fillStrong: '#3B3838', onFillStrong: '#FFFFFF' },
    dark:      { fillStrong: '#E5E5E5', onFillStrong: '#1F1F1F' },
    pureWhite: '#FFFFFF',
  },
  chart: ['#3B3838', '#535353', '#8C8C8C', '#BFBFBF', '#D9D9D9'],
  semantic: {
    light: { success: '#2D5A3D', warn: '#7A5A1F', danger: '#7A2D2D' },
    dark:  { success: '#8FBF9F', warn: '#D9B97A', danger: '#D98F8F' },
  },
};
```

---

## 8. Pre-Publish Checklist

산출물 발행 전 점검 항목.

### 공통

- [ ] 채도 있는 색(파랑·빨강·녹색·노랑 등) 사용이 0건인가?
- [ ] 그라디언트 fill 사용이 0건인가?
- [ ] 차트 시리즈가 위 5색 범위 내인가?
- [ ] 시맨틱 색을 사용한 경우 텍스트·작은 아이콘에만 적용됐는가?
- [ ] 컬러 사진·일러스트는 모두 흑백으로 변환됐는가?
- [ ] 인접 토큰 간 명도 차가 30% 이상 확보됐는가?

### 정적 문서 (PPT·PDF·인쇄)

- [ ] 배경이 흰색(`#FFFFFF`)인가? (정적 산출물은 dark 변종 사용 금지)
- [ ] 본문 텍스트가 `#535353` 또는 `#1F1F1F`인가?
- [ ] 헤더 바를 사용한 경우 `#3B3838`인가?

### 동적 UI (웹 앱·콘솔)

- [ ] light / dark 양 변종에서 모두 텍스트 ↔ 배경 대비 ≥ 4.5:1 (본문) / ≥ 7:1 (긴 본문) 가 확보됐는가?
- [ ] 다크 변종에서 fill 위 글자가 fill 과 같은 톤으로 깔리는 영역이 없는가? (헬퍼 토큰 `fillStrong` / `onFillStrong` 쌍 사용 권장)
- [ ] `.text-white` 류 "항상 흰색" 의도 셀렉터는 `pureWhite` anchor 를 참조하는가?
- [ ] OS `prefers-color-scheme` 만으로 진입한 다크 초기 paint 에서도 색이 깨지지 않는가?

---

## 9. Accessibility Notes

본 팔레트는 무채색 기반이므로 색약·색맹 사용자에게도 **명도 대비**만으로 정보를 전달한다.

주요 텍스트 조합의 명도 대비(WCAG 기준):

#### Light

| Foreground | Background | Contrast | WCAG Level |
|-----------|------------|----------|------------|
| `ink` (#1F1F1F) | `white` (#FFFFFF) | 16.1 : 1 | AAA |
| `primary` (#535353) | `white` (#FFFFFF) | 7.6 : 1 | AAA |
| `mutedText` (#8C8C8C) | `white` (#FFFFFF) | 3.5 : 1 | AA (Large) |
| `white` (#FFFFFF) | `headerBar` (#3B3838) | 11.6 : 1 | AAA |
| `white` (#FFFFFF) | `primary` (#535353) | 7.6 : 1 | AAA |

#### Dark

| Foreground | Background | Contrast | WCAG Level |
|-----------|------------|----------|------------|
| `ink` (#FFFFFF) | `white` (#1F1F1F) | 16.1 : 1 | AAA |
| `primary` (#D9D9D9) | `white` (#1F1F1F) | 13.3 : 1 | AAA |
| `mutedText` (#A6A6A6) | `white` (#1F1F1F) | 7.8 : 1 | AAA |
| `onFillStrong` (#1F1F1F) | `fillStrong` (#E5E5E5) | 14.4 : 1 | AAA |
| `primary` (#D9D9D9) | `surface` (#2A2A2A) | 11.5 : 1 | AAA |

본문 텍스트는 항상 AAA 등급(`primary` 이상)을 사용하며, `mutedText`는 보조 캡션 등 큰 텍스트에 한정한다. 다크 변종은 `mutedText` 도 AAA 를 만족하도록 톤업되어 있으므로 본문에 사용해도 무방하다.

---

## 10. Quick Reference Card

```
┌──────────────────────────────────────────────────────────────┐
│  RX BRAND COLORS                                             │
│──────────────────────────────────────────────────────────────│
│  Core           #535353  #3B3838  #FFFFFF                    │
│                                                              │
│                       LIGHT          DARK                    │
│  Page bg              #FFFFFF        #1F1F1F                 │
│  Surface              #F7F7F7        #2A2A2A                 │
│  Subtle               #EDEDED        #333333                 │
│  Divider              #D9D9D9        #404040                 │
│  Border               #BFBFBF        #595959                 │
│  Muted text           #8C8C8C        #A6A6A6                 │
│  Body text            #535353        #D9D9D9                 │
│  Strong text (ink)    #1F1F1F        #FFFFFF                 │
│  Header bar           #3B3838        #4A4A4A                 │
│  fillStrong           #3B3838        #E5E5E5                 │
│  onFillStrong         #FFFFFF        #1F1F1F                 │
│                                                              │
│  Chart Series   #3B3838 #535353 #8C8C8C #BFBFBF #D9D9D9      │
│  Status Ink     L:  ✓#2D5A3D  ⚠#7A5A1F  ✕#7A2D2D            │
│                 D:  ✓#8FBF9F  ⚠#D9B97A  ✕#D98F8F            │
│──────────────────────────────────────────────────────────────│
│  NO color · NO gradient · NO exception                       │
│  정적 산출물 → light only.  동적 UI → light + dark.          │
└──────────────────────────────────────────────────────────────┘
```

---

*RX Inc. · Brand Color Palette · v1.1 · 2026-05-18*
