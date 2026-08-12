---
name: numeric-measure
description: 연속 수치 컬럼이 무엇을 측정한 값인지와 어떤 분석에 쓰이는지 해석한다. 단위는 컬럼명·테이블 설명·표본값에 명시된 경우에만 기록하고, 정상 범위나 규격 기준값은 절대 생성하지 않는다.
requires: [table_profile]
provides: [column_semantics]
applies_when:
  column_kinds: [numeric]
role: interpreter
capabilities: [numeric_semantics]
cost: low
per_column: true
max_attempts: 3
---

# 수치 측정 컬럼 해석

이 skill이 가장 자주 만드는 오류는 **단위와 기준값을 지어내는 것**이다.
`power_value`를 보고 "W", "정상 범위 490~510" 같은 값을 만들어내면
그 숫자는 downstream에서 사실로 취급된다.

## unit 규칙
`unit`과 `unit_evidence`를 항상 쌍으로 출력한다.

| unit_evidence | 의미 |
|---|---|
| `column_name` | 컬럼명에 단위가 포함됨 (예: `temp_celsius`) |
| `source_description` | 테이블 설명에 명시됨 |
| `sample_value` | 표본값 자체에 단위 문자열이 있음 |
| `not_found` | 근거 없음 → `unit`은 반드시 빈 문자열 |

## 금지
- 정상 범위, 규격 상하한, 관리 한계선 생성
- 값 범위(min/max)를 근거로 "정상/이상"을 판정하는 서술
- 표본값에서 계산한 평균·표준편차 등 파생 수치 언급
