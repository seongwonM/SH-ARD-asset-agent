---
name: dependency-check
description: 컬럼 간 함수 종속(A가 정해지면 B도 정해짐)을 데이터로 직접 검사해 정합성 사실을 수집한다. LLM을 쓰지 않고 후보 쌍을 전수 검사하며, 입도 판정과 최종 요약이 이 사실을 근거로 쓴다.
requires: [table_profile]
provides: [constraints]
applies_when:
  min_columns: 2
role: observer
capabilities: [functional_dependency_scan]
cost: free
per_column: false
max_attempts: 1
---

# 컬럼 간 종속성 검사

LLM을 사용하지 않는다. 이 사실은 데이터에서 직접 세는 것이고, 추론할 대상이 아니다.

데이터팀의 정합성 점검 항목은 보통 **Granularity / PK / Null / Dependency** 네 가지다.
앞의 셋은 이미 프로파일과 입도 판정이 다루지만, Dependency만 비어 있었다.
이 skill이 그 빈칸을 채운다.

## 검사 대상

식별자·범주형 컬럼 쌍만 본다. 수치 측정값은 함수 종속의 좌변으로 의미가 없다.
컬럼이 많으면 조합이 폭발하므로 `max_pairs`로 잘라내고, 잘린 사실은
"검사하지 않음"으로 남긴다. **검사하지 않은 것을 성립한다고 말하지 않는다.**

## 왜 유용한가

- `line_id`가 `equipment_id`에 종속이면 두 컬럼은 같은 위계의 다른 층이다
- 종속이 성립하는 컬럼은 입도 키 후보에서 빠져야 한다
- 정규화 여부를 사람이 판단할 근거가 된다

## 금지
- 종속이 성립한다고 인과나 업무 규칙을 주장하지 않는다. 관측된 사실만 기록한다.
