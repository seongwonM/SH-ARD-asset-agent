---
name: profile-table
description: 테이블을 읽어 컬럼별 타입·분포·표본값을 산출하고 각 컬럼에 kind(identifier/temporal/numeric/categorical/free_text/spatial)를 부여한다. LLM을 쓰지 않으며 모든 후속 skill의 근거 집합이 된다. 항상 가장 먼저 실행된다.
requires: []
provides: [table_profile]
applies_when:
  always: true
cost: free
per_column: false
max_attempts: 1
---

# 테이블 프로파일링

LLM을 사용하지 않는다. 이 skill의 출력이 이후 모든 가드레일의 "허용된 사실 집합"이 되므로,
추정값을 넣으면 그 오염이 파이프라인 전체로 퍼진다. 계산 가능한 값만 채운다.

## kind 판정 규칙

| kind | 조건 |
|---|---|
| `temporal` | datetime dtype이거나 파싱 성공률 >= 0.9 |
| `identifier` | 컬럼명이 `_id/_key/_no/_code`로 끝나거나 distinct_ratio >= 0.98 |
| `numeric` | 수치 dtype이고 identifier가 아님 |
| `categorical` | distinct_count <= 30 이고 distinct_ratio <= 0.05 |
| `free_text` | 텍스트이고 평균 길이 >= 40 |
| `spatial` | 컬럼명에 lat/lon/geo/region/site/line/zone 포함 |

판정이 겹치면 위 표의 순서대로 우선한다.
