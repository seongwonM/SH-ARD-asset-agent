# ROADMAP — 데이터팀 12영역 매핑

Joshua Kim, "데이터팀이 Agentic AI를 도입해야 할 12개의 영역"(2026)의 분류를
이 에이전트에 대조한 결과. 전체를 하려는 게 아니라 **어디까지가 이 자산의
범위이고 어디부터가 다른 에이전트의 일인지** 긋는 것이 목적이다.

원문의 핵심 주장 하나가 이 프로젝트의 전제와 같다.

> 판단을 Skill로 코드화하고, 그 Skill을 Agent가 자율적으로 호출하게 되면,
> 팀의 전문성이 개인에 귀속되지 않고 시스템에 축적된다.

`SKILL.md`의 "금지" 절이 정확히 그 코드화다. "단위를 지어내지 마라",
"표본에 없는 값을 설명하지 마라"는 누군가 리뷰에서 반복해 지적하던 판단이고,
지금은 파일에 적혀 있어 매 실행마다 강제된다.

---

## 이미 구현된 것

### 영역 5 · Catalog — 이 에이전트의 본체

> 목표: 메타데이터는 "작성하는 것"이 아니라 "자동으로 최신화되는 것"

| 원문 아이디어 | 대응 |
|---|---|
| Catalog as a Code 명세서 생성 | `synthesize-context` → `asset_context` |
| PII 태깅 자동화 | `pii-detection` |
| Business Glossary 동기화 | `glossary-align` |

원문은 카탈로그가 최신화되지 않는 원인을 **작성 공수**로 본다.
여기에 한 가지를 더한다: **틀린 카탈로그는 없는 것보다 나쁘다.**
그래서 `verify-context`가 `verified/refuted/unverified`를 구분해 남긴다.
`unverified` 목록이 곧 사람이 봐야 할 큐다.

### 영역 2 · Transform — 정합성 검사

> 데이터 정합성 검사 (Granularity, PK, Null, Dependency)

원문이 한 줄로 묶은 네 항목이 이 에이전트에서는 각각 다른 계층에 있다.

| 항목 | 대응 | 검증 방식 |
|---|---|---|
| Granularity | `grain-resolution` | `UNIQUENESS` probe로 키 조합 반증 |
| PK | `identifier-link` | 동일. `primary` 주장은 유일성 0.99 요구 |
| Null | `profile-table` | `null_ratio` 산출 |
| **Dependency** | `dependency-check` | `FUNCTIONAL_DEP` probe 전수 검사 |

Dependency만 비어 있어서 이번에 채웠다. LLM을 쓰지 않는다 —
세면 되는 사실이고, 세는 편이 싸고 정확하다.

### 영역 1 · EL / 영역 12 · Governance — PII

> 개인정보 자동 식별 및 마스킹 강제 / PII 접근 로그 분석

`pii-detection`이 컬럼 단위 판정과 등급(`direct`/`quasi`/`none`)을 낸다.
마스킹 강제와 접근 로그는 이 에이전트 범위 밖 — 태그를 소비하는 쪽의 일이다.

판정 설계에서 한 가지를 비대칭으로 뒀다. `direct`만 정규식 probe로 반증하고
`quasi`는 검사하지 않는다. `quasi`를 엄격히 검사하면 애매한 컬럼이 `none`으로
떨어지는데, **그 방향의 실수가 더 비싸다.**

---

## 다음 (Group Context 단계)

단일 테이블로는 불가능하고 여러 자산을 함께 봐야 하는 것들.

### 영역 5 · 중복 지표 탐지
> "주문수"가 3개 마트에 미묘하게 다르게 정의된 경우

`asset_context.search_text` 임베딩 유사도로 후보를 뽑고,
**컬럼 값 overlap probe로 실제 같은 것을 세는지 확인**한다.
임베딩만으로 "같은 지표"라고 단정하는 것보다 훨씬 강하다.
→ `ProbeKind.VALUE_OVERLAP` 추가 필요

### 영역 5 · Lineage 기반 설명 생성
> 상류 테이블/컬럼 명세 + SQL 변환 로직 → 하류 컬럼 명세 자동 작성

`linkage` 슬롯에 이미 컬럼별 role과 유일성이 쌓인다.
여러 자산의 linkage를 모으면 조인 후보가 나온다.
조인 가능성 주장도 값 overlap으로 반증 가능하다.

### 영역 2 · Deprecated 모델 탐지 / 영역 4 · 사용 빈도 기반 마트화
쿼리 로그가 입력이라 이 에이전트의 입력 계약(DataFrame) 밖이다.
별도 에이전트로 두고 `asset_context`를 참조하게 하는 편이 낫다.

---

## 범위 밖 (다른 에이전트의 일)

| 영역 | 이유 |
|---|---|
| 3 이벤트 수집, 6 BI, 7 Analytics, 9 Reverse ETL | 자산 해석이 아니라 활용 |
| 4 DQ 중 이상치 탐지·Freshness | 런타임 모니터링. 자산 컨텍스트는 정적 사실 |
| 8 Data Science | 모델 주변 업무 |
| 10 Infra & Cost, 11 Onboarding | 조직 기반 |

원문의 "공통 빌딩블록부터 쌓기"에 해당하는 것이 이 에이전트다.
`asset_context`는 위 영역 다수가 공통으로 참조할 재료다.

---

## 공개 skill 카탈로그 대조

claudeskills.info의 데이터 분석 skill 상위 15개를 봤다.
**대부분은 직접 쓸 수 없다.** pandas 조작(`pandas-pro`), 시각화
(`data-visualization`), GA4 트래킹(`analytics`) 같은 범용 도구라
자산 컨텍스트 생성과 목적이 다르다.

빌려올 만한 것은 두 가지다.

### 1. "fail closed" 원칙
`k-dense-ai/exploratory-data-analysis`가 명시한다 —
*"Other domain formats are reference-only and unknown formats fail closed."*

우리 `generic-column`은 분류 불명 컬럼을 fallback으로 처리하는데,
지금은 LLM에게 "표본값 근거로 판단하라"고만 한다.
**모르면 모른다고 하고 멈추는 쪽**이 더 안전하다.
`SKILL.md`에 "판단 근거가 표본에 없으면 meaning을 비우고 usage만 남긴다"를
추가하는 것이 후보다.

### 2. description에 트리거 표현 나열
상위 skill들은 description에 *"Also use when the user mentions ..."*로
호출 조건을 촘촘히 적는다. 우리 플래너는 결정론적이라 이 방식이 직접
필요하지는 않지만, **LLM tie-break가 붙는 순간 이 문장이 유일한 판단 근거**가
된다. `needs_tiebreak`를 연결할 때 description을 함께 손봐야 한다.

### 안 빌려온 것
`statistical-analysis`의 가설검정·이상치 탐지는 매력적이지만,
자산 컨텍스트에 "이 컬럼은 정규분포를 따른다" 같은 통계 주장을 넣으면
표본 크기에 따라 결과가 흔들린다. probe로 반증 가능한 사실만 담는다는
원칙과 충돌해서 보류했다.
