---
name: categorical-code
description: 값 종류가 제한된 코드·상태·구분 컬럼이 어떤 분류 체계를 나타내는지 해석한다. 표본에 실제로 등장한 값만 설명하고, 관측되지 않은 카테고리를 나열하지 않는다.
requires: [table_profile]
provides: [column_semantics]
applies_when:
  column_kinds: [categorical]
role: interpreter
capabilities: [categorical_semantics]
cost: low
per_column: true
max_attempts: 3
---

# 범주형 컬럼 해석

## 금지
- 표본에 없는 값 설명. `output_judge`에서 "정상"만 관측됐다면 "이상" 값의 의미를 쓰지 않는다.
- 카테고리 간 순서·심각도 위계 부여
- 코드 체계 표준(예: ISO 코드표) 매핑 추측

## 출력
`observed_values`에는 표본에서 확인된 값만 `값: 의미` 형태로 넣는다.
의미가 불확실한 값은 목록에서 제외한다.
