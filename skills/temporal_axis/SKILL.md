---
name: temporal-axis
description: 날짜/시각 컬럼이 어떤 시점을 기록하는지, 시간 해상도가 무엇인지 해석한다. 표본값에서 확인 가능한 해상도만 인정하며, 시각 정보가 없는 표본에 시/분 해상도를 부여하지 않는다.
requires: [table_profile]
provides: [column_semantics]
applies_when:
  column_kinds: [temporal]
cost: low
per_column: true
max_attempts: 3
---

# 시간축 컬럼 해석

## 판단할 것
- 이 시점이 무엇의 시점인가 (발생, 기록, 유효 시작, 마감 등)
- 표본값에서 실제로 구분 가능한 최소 시간 단위

## 금지
- 표본에 `HH:MM`이 없으면 Hour 이하 해상도를 주장하지 않는다. 날짜만 있으면 Day가 상한이다.
- 데이터가 수집된 기간의 의미(분기 마감, 성수기 등)를 추측하지 않는다.
- 다른 컬럼과의 시간 순서 관계를 단정하지 않는다.

## 출력
`resolution`은 Year/Quarter/Month/Week/Day/Hour/Minute/Second 중 하나.
확신이 없으면 더 거친 단위를 택한다.
