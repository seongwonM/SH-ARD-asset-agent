---
name: verify-context
description: 지금까지 채워진 슬롯의 주장들을 데이터로 재검사해 검증 리포트를 만든다. 컬럼 의미가 확정된 뒤 최종 합성 직전에 실행되며, 반증된 주장과 미검증 주장을 구분해 남긴다. LLM을 쓰지 않는다.
requires: [table_profile, column_semantics]
provides: [verification]
applies_when:
  always: true
cost: free
per_column: false
max_attempts: 1
---

# 검증 리포트

LLM을 사용하지 않는다. 이미 board에 있는 주장들을 데이터로 다시 확인할 뿐이다.

## 왜 executor의 probe 검증과 별도인가

executor의 probe는 **생성 시점**에 그 skill의 주장만 본다.
이 skill은 **전체가 모인 뒤** 교차 검사를 한다. 예를 들어
- identifier가 reference라고 했는데 grain의 key_columns에 들어가 있는가
- 여러 컬럼 의미가 서로 모순되는가

또한 어떤 주장이 아예 검증되지 않았는지(probe가 붙지 않은 주장)를 집계한다.
"틀린 걸 어떻게 판단하는가"의 답은 결국 **검증된 주장과 검증되지 않은 주장을
구분해서 남기는 것**이다. 전부 참이라고 말하는 리포트는 쓸모가 없다.

## 출력
- `verified`: probe 통과 주장
- `refuted`: probe 반증 주장 (최종 컨텍스트에서 제외되어야 함)
- `unverified`: 검사 수단이 없는 주장 (사람이 봐야 하는 목록)
- `coverage`: 검증률
