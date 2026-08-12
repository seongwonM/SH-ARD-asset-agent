---
name: grain-resolution
description: 이 테이블의 한 행이 무엇을 의미하는지(입도)를 판정한다. 식별자와 시간축 컬럼의 해석 결과를 종합해 "장비별 공정 실행 1건" 같은 단위를 도출한다. 컬럼 의미가 절반 이상 채워진 뒤에 실행된다.
requires: [table_profile]
provides: [grain]
applies_when:
  min_columns: 2
cost: low
per_column: false
max_attempts: 2
---

# 행 입도 판정

컬럼별 의미가 모여도 "이 테이블의 한 행이 무엇인가"는 별도 판단이 필요하다.
입도가 틀리면 요약 전체가 어긋난다.

## 판단 재료
- `primary` role 식별자 → 그 개체 1건이 곧 1행
- `reference` 식별자 + 시간 컬럼 → 개체별 시점 기록
- 식별자 없음 + 시간 컬럼 → 시점별 집계값일 가능성

## 금지
- 실제 유일성 검증 없이 복합키를 단정하지 않는다. 근거가 프로파일에 있는지 확인한다.
