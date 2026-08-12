---
name: quality-risk-assessment
description: 테이블 프로파일과 이미 확인된 제약을 바탕으로 결측, 중복, 고카디널리티, enum drift 가능성 같은 데이터 품질 리스크를 요약한다. LLM을 쓰지 않는 테이블 수준 skill이다.
requires: [table_profile]
provides: [quality_risks]
applies_when:
  min_columns: 1
role: observer
capabilities: [quality_risk_scan]
cost: free
per_column: false
max_attempts: 1
---

# 데이터 품질 리스크 점검

이 skill은 "무엇이 사실인가"보다 "어디가 위험한가"를 요약한다.
프로파일 통계로 바로 확인 가능한 위험만 기록한다.

## 다루는 리스크
- null 비율이 높은 컬럼
- 거의 전부 다른 값이라 조인/집계에 불안정한 컬럼
- 값 종류가 너무 많은 범주형 컬럼
- 중복 위험이 있는 테이블

## 금지
- 원인 추정
- 비즈니스 영향 과장
- 실제로 계산하지 않은 비율/건수 생성
