---
name: join-key-analysis
description: 식별자와 코드성 컬럼을 대상으로 조인 키 후보 강도를 평가한다. 유일성, 결측 비율, 값 반복 정도를 바탕으로 entity key, reference key, grouping code 같은 역할 후보를 기록한다. LLM을 쓰지 않는 테이블 수준 skill이다.
requires: [table_profile]
provides: [join_candidates]
applies_when:
  min_columns: 1
role: observer
capabilities: [join_candidate_scan]
cost: free
per_column: false
max_attempts: 1
---

# 조인 후보 키 평가

이 skill은 실제 외부 테이블명을 추정하지 않는다.
대신 "이 컬럼을 조인 키로 써도 되는가"에 가까운 실무적 판단 근거를 남긴다.

## 기록할 것
- 역할 후보: `entity_key`, `reference_key`, `grouping_code`, `weak_candidate`
- 근거: 고유값 비율, 결측 비율
- 주의점: 값이 너무 많음, 결측이 큼, 반복이 과도함

## 금지
- 외부 마스터 테이블명 추정
- 1:N, N:M 관계 단정
