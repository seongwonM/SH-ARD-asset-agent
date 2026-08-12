---
name: value-pattern-profile
description: 식별자·텍스트·미분류 컬럼의 값 패턴을 요약해 고정 자릿수 코드, 영숫자 혼합, 이메일, 전화번호, 날짜 문자열 같은 형식을 기록한다. LLM을 쓰지 않는 테이블 수준 skill이다.
requires: [table_profile]
provides: [value_patterns]
applies_when:
  min_columns: 1
role: observer
capabilities: [value_pattern_scan]
cost: free
per_column: false
max_attempts: 1
---

# 값 패턴 요약

이 skill은 문자열 값의 형식을 요약한다.
의미를 해석하는 것이 아니라, downstream에서 규칙을 만들기 쉽게 패턴을 남기는 것이 목적이다.

## 다루는 패턴
- 숫자만
- 영숫자 혼합
- 고정 자릿수
- 이메일
- 전화번호
- 날짜 문자열
- JSON/path 유사 형식

## 금지
- 형식만 보고 개인정보 확정
- 값 일부를 예시로 창작
