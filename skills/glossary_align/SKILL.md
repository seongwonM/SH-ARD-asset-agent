---
name: glossary-align
description: 제공된 컬럼 설명에서 사내 표준용어를 찾아 컬럼과 정렬하고, 표준용어가 없는 컬럼을 미정의 목록으로 남긴다. 데이터 사전이나 스키마 설명이 입력으로 주어졌을 때만 의미가 있으며, 없으면 아무것도 만들지 않는다.
requires: [table_profile]
provides: [glossary]
applies_when:
  always: true
role: interpreter
capabilities: [glossary_alignment]
cost: low
per_column: false
max_attempts: 2
---

# 표준용어 정렬

데이터 카탈로그의 실패 원인은 대부분 **작성 공수가 커서 최신화되지 않는 것**이다.
표준용어 매핑을 사람이 손으로 유지하면 결국 낡는다. 이 skill은 이미 주어진
컬럼 설명에서 표준용어를 **추출**할 뿐, 새로 만들지 않는다.

## 절대 금지

- **표준용어를 창작하지 않는다.** 입력 설명에 문자 그대로 등장한 용어만 쓴다.
  "설비ID"가 설명에 있으면 쓸 수 있고, 없는데 "장비식별자"를 지어내면 안 된다.
  카탈로그에 없는 용어가 표준용어로 등록되면 그 순간 카탈로그가 오염된다.
- 표준용어가 없는 컬럼은 **비워두고 `unmapped`에 남긴다.** 억지로 채우지 않는다.
  미정의 목록이야말로 사람이 표준용어를 새로 정의해야 할 큐다.

## 출력
- `mappings`: {컬럼: 표준용어} — 설명에 실재하는 용어만
- `unmapped`: 표준용어를 찾지 못한 컬럼 목록
