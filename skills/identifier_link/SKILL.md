---
name: identifier-link
description: 식별자 컬럼이 무엇을 가리키는지와 유일 식별자인지 반복 참조 키인지 판별한다. 참조 대상은 컬럼명이나 테이블 설명에 근거가 있을 때만 기록하며, 다른 테이블 이름을 추측하지 않는다. linkage 슬롯에 기여해 자산 간 연결 후보를 남긴다.
requires: [table_profile]
provides: [column_semantics, linkage]
applies_when:
  column_kinds: [identifier]
cost: low
per_column: true
max_attempts: 3
---

# 식별자 컬럼 해석

## role 판정
| role | 조건 |
|---|---|
| `primary` | distinct_ratio >= 0.99. 한 행을 유일하게 지목 |
| `reference` | 값이 반복됨. 외부 개체를 가리킴 |
| `business` | 업무상 코드. 유일성은 보장되지 않음 |

`distinct_ratio`가 프로파일에 이미 있다. 이를 무시하고 `primary`를 주장하면 검증에서 걸린다.

## 금지
- 참조 대상 테이블명 추측. `equipment_id`를 보고 "equipment_master를 참조한다"고 쓰지 않는다.
  실제 조인 관계 확인은 이 에이전트의 범위 밖이다.
- 카디널리티(1:N 등) 단정
