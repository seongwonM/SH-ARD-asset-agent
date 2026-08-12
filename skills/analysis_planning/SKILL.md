---
name: analysis-planning
description: 결정론적으로 수집된 evidence artifact를 읽고 지금 무엇이 부족한지, 다음에 어떤 분석이 필요한지 계획을 세운다. observer 결과가 모인 뒤 실행되며, 추가 observer/interpreter 작업 요청과 해석 우선순위를 analysis_plan 슬롯에 기록한다.
requires: [table_profile, distribution_profile, value_patterns, join_candidates, constraints, quality_risks, compliance]
provides: [analysis_plan]
applies_when:
  always: true
role: deliberator
capabilities: [analysis_planning, request_followup]
cost: low
per_column: false
max_attempts: 2
---

# Evidence-first 분석 계획

이 skill의 역할은 직접 사실을 만드는 것이 아니라, 이미 수집된 evidence를 읽고
"이제 무엇을 더 해야 하는가"를 정하는 것이다.

## 원칙
- raw dataframe을 추측하지 않는다. board의 artifact와 profile만 근거로 쓴다.
- 다음 작업은 구체적인 slot 단위로 제안한다.
- 이미 충분한 증거가 있는 슬롯은 다시 요청하지 않는다.
- 자연어 요약보다 분석 순서를 우선 결정한다.

## 출력
- `focus`: 지금 해석의 초점
- `ready_slots`: 바로 진행 가능한 slot 목록
- `requested_slots`: 추가로 더 보고 싶은 slot 목록
- `rationale`: 왜 그런 순서가 필요한지 짧게 설명
