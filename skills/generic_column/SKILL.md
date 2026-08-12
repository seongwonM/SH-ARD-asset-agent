---
name: generic-column
description: 전용 skill이 없는 컬럼(자유 텍스트, 공간 정보, 분류 불명)의 의미를 일반적으로 해석한다. 다른 skill이 담당하지 않는 컬럼만 처리하는 fallback이며, 특정 종류에 특화된 skill이 있으면 그쪽이 우선한다.
requires: [table_profile]
provides: [column_semantics]
applies_when:
  column_kinds: [unknown, free_text, spatial]
cost: low
per_column: true
max_attempts: 2
---

# 일반 컬럼 해석

전용 skill이 없는 컬럼을 위한 fallback이다.
이 skill이 자주 호출된다면 그 kind에 맞는 전용 skill을 만들어야 한다는 신호다.

## 금지
- 컬럼명에서 연상되는 도메인 지식 주입. `zone`을 보고 "클린룸 등급"을 추측하지 않는다.
- 값의 단위, 코드 체계, 표준 규격 추정
