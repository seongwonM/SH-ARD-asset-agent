# Evidence-First Data Analysis Agent

이 문서는 2026-08-12 기준 현재 아키텍처를 설명한다.

## 목표

이 에이전트는 `가설 먼저 -> 사후 반증`보다 `evidence first`를 우선한다.

핵심 원칙:

- rule-based observer가 먼저 최대한 많은 사실을 계산한다.
- LLM은 raw data보다 evidence artifact를 보고 해석한다.
- LLM은 다음에 어떤 분석을 더 해야 하는지도 제안할 수 있다.
- 최종 합성은 검증 결과와 evidence를 넘어서지 않는다.

## 실행 루프

그래프 노드는 여전히 둘뿐이다.

1. `plan`
2. `act`

하지만 내부 의미는 바뀌었다.

1. observer skill이 evidence를 채운다.
2. deliberator skill이 evidence를 읽고 `analysis_plan`을 만든다.
3. interpreter skill이 필요한 column/table 해석을 수행한다.
4. verifier가 probe와 교차 검사를 수행한다.
5. synthesizer가 최종 `asset_context`를 만든다.

즉:

`Rule-based Observation -> Evidence Board -> LLM Deliberation -> Interpretation -> Verification -> Synthesis`

## Board

`Board`는 이제 세 층을 가진다.

- `values`: 단일 슬롯 값
- `keyed`: 컬럼별/키별 슬롯 값
- `artifacts`: 관측/해석/검증 산출물 목록

artifact는 공통 스키마를 따른다.

- `artifact_type`
- `producer`
- `role`
- `slot`
- `key`
- `scope`
- `payload`
- `evidence`
- `confidence`

새 rule-based 계산을 추가할 때는 board schema를 바꾸기보다 새 artifact producer를 추가하는 쪽을 우선한다.

## Skill Role

모든 skill은 frontmatter에 `role`을 가진다.

- `observer`
- `interpreter`
- `verifier`
- `synthesizer`
- `deliberator`

planner는 이 role을 사용해 observer를 먼저 선호한다.

## Analysis Plan

`analysis-planning` skill은 evidence artifact를 읽고:

- 지금 바로 진행 가능한 슬롯
- 추가로 더 보고 싶은 슬롯
- 현재 분석 초점

을 `analysis_plan` 슬롯에 기록한다.

이 skill은 `requests`와 `analysis_needs`도 반환할 수 있다.

의도는 이것이다:

- 수치형 컬럼 3개를 보고 단순 요약으로 끝내지 않는다.
- evidence를 본 LLM이 "여기서는 grain을 먼저 봐야 한다", "identifier 해석이 필요하다" 같은 후속 작업을 제안한다.
- 이후 새 observer를 추가해도 같은 루프에 편입된다.

## 유지보수 원칙

새 분석 로직을 넣을 때 우선순위:

1. observer로 구현 가능한가
2. artifact만 추가하면 되는가
3. planner 하드코딩 없이 frontmatter/capability로 연결되는가
4. probe로 반증 가능한가

권장 추가 순서:

1. `skills/<name>/SKILL.md`에 `role`, `capabilities`, `requires`, `provides` 선언
2. `handler.py`에서 결정론적 계산 또는 구조화된 해석 수행
3. 가능한 경우 `Artifact`를 명시적으로 반환
4. probe가 필요한 주장이라면 `claims`에 추가
5. README/HANDOFF에 새 skill의 목적과 리스크 기록

## 다른 환경에서 이어받을 때

확인 순서:

1. Python 3.12 이상
2. `venv`, `pip`, `pytest` 사용 가능 여부
3. `make test` 또는 `python -m pytest -q`
4. `skills/analysis_planning/` 포함 여부
5. runner 출력에 `analysis_plan`, `analysis_artifacts`가 노출되는지 확인

## 현재 한계

- 아직 모든 skill이 artifact를 수동 생성하지는 않는다. executor가 contribution에서 자동 변환한다.
- `analysis-planning`은 첫 deliberator다. 후속으로 `relation_deep_dive`, `time_series_followup` 같은 observer를 더 추가할 수 있다.
- planner는 role 기반 가중치를 추가했지만 아직 정보 이득 추정까지 하지는 않는다.
