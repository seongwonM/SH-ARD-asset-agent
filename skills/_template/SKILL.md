---
name: my-skill
description: 이 skill이 언제 필요한지 한두 문장. 플래너가 보는 유일한 설명이므로 "무엇을 하는가"보다 "어떤 상황에서 골라야 하는가"를 쓴다.
requires: [table_profile]
provides: [column_semantics]
applies_when:
  column_kinds: [numeric]
  # min_columns: 2
  # min_rows: 100
  # column_name_patterns: ["temp", "press"]
  # always: false
cost: low
per_column: true
max_attempts: 3
---

# 제목

이 본문은 skill이 선택된 뒤에만 로드되어 handler의 system 프롬프트가 된다.
플래너 단계에서는 위 frontmatter만 읽으므로, 본문이 길어도 플래닝 비용은 늘지 않는다.

## 판단할 것
- (LLM이 결정해야 할 항목을 구체적으로)

## 금지
- (근거 없이 만들어내기 쉬운 것을 명시적으로 나열)
- 표본에 없는 값 설명, 추정 단위, 규격 기준값, 타 자산과의 관계

## 출력 규칙
- (Literal 필드의 선택지와 각 값의 의미)
