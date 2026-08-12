---
name: distribution-profile
description: 수치형과 범주형 컬럼의 값 분포를 요약해 zero-heavy, 음수 포함, 범주 쏠림 같은 특성을 기록한다. LLM을 쓰지 않는 테이블 수준 skill이다.
requires: [table_profile]
provides: [distribution_profile]
applies_when:
  min_columns: 1
role: observer
capabilities: [distribution_scan]
cost: free
per_column: false
max_attempts: 1
---

# 컬럼 분포 요약

이 skill은 컬럼 의미를 새로 해석하지 않는다.
이미 있는 프로파일과 실제 값을 바탕으로 분포 특성만 기록한다.

## 다루는 대상
- 수치형: 0 비율, 음수 포함 여부, 상수 컬럼 여부, 분위수
- 범주형: 상위 값 집중도, 대표 값 목록

## 금지
- 분포만 보고 이상/정상 판정
- 업계 기준치 추정
- 원인 해석
