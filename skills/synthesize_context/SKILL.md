---
name: synthesize-context
description: 채워진 컬럼 의미·행 단위·연결 정보를 종합해 자산 전체의 주제, 요약, 검색어를 생성하고 최종 asset_context를 조립한다. 새로운 사실을 만들지 않고 이미 검증된 슬롯만 합성한다. 파이프라인의 마지막 skill이다.
requires: [table_profile, column_semantics, grain, verification, constraints, quality_risks, compliance, glossary]
provides: [topic, summary, search_terms, asset_context]
applies_when:
  always: true
role: synthesizer
capabilities: [asset_context_synthesis]
cost: medium
per_column: false
max_attempts: 3
---

# 최종 컨텍스트 합성

## 원칙
이 단계는 **새로운 해석 단계가 아니라 합성 단계**다.
컬럼별 해석에서 확인되지 않은 내용을 여기서 처음 도입하지 않는다.
슬롯에 없는 사실이 요약에 등장하면 그것은 이 skill이 만들어낸 것이다.

## 생성물
| 항목 | 내용 |
|---|---|
| `topic` | 2~3 단어 |
| `summary` | 무엇을 담고 어떤 범위이며 어디에 쓰이는지. 3~4문장 |
| `search_terms` | 검색 색인용. 동의어·대체 표현 확장은 허용 |

`search_terms`만 확장이 허용된다. 검색 재현율이 목적이므로 일반 명사 확장까지 막으면
목적을 해친다. 단 수치·기준값·타 자산 관계는 여기서도 만들지 않는다.

## 행 단위 미확정 시
`grain` 슬롯이 비어 있으면 요약에서 행 단위를 단정하지 않고 컬럼 구성 중심으로 기술한다.
