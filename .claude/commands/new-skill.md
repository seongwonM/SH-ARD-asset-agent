---
description: 새 skill을 스캐폴딩하고 계약에 맞게 검증한다
argument-hint: <skill-name> [담당할 컬럼 종류나 작업]
---

`$1` skill을 추가한다. 대상: $2

## 절차

1. `skills/_template/`을 `skills/<snake_name>/`으로 복사한다.
2. `SKILL.md` frontmatter를 채운다.
   - `description`은 플래너가 보는 유일한 설명이다. "무엇을 하는가"가 아니라
     **"어떤 상황에서 이 skill을 골라야 하는가"**를 쓴다.
   - `requires`/`provides`로만 실행 순서를 표현한다. 코드로 순서를 고정하지 않는다.
   - `applies_when`은 결정론적 조건만 쓴다.
3. 본문에 **금지 항목**을 구체적으로 나열한다. 이 skill이 근거 없이 만들어내기
   쉬운 것이 무엇인지 생각해서 적는다.
4. `handler.py`를 작성한다.
   - Pydantic 모델: `Optional`/`Union` 금지, 라벨은 `Literal`, `extra="forbid"`
   - 데이터로 확인 가능한 주장을 하면 **반드시 `VerifiableClaim`에 probe를 첨부**한다.
     handler 안 if문으로 검사하지 말 것 — probe로 선언하면 실측값이 재시도 힌트가 된다.
   - 프롬프트를 만들기 전에 `deps.probe()`로 사실을 먼저 확인해 LLM에 제공한다.
5. `tests/fixtures.py`의 `MockDeps.structured()`에 새 출력 모델 분기를 추가한다.
6. `make test`로 검증한다. 기존 테스트가 깨지면 계약을 어긴 것이다.

## 확인할 것

- `planner.py`, `graph.py`, `executor.py`를 **수정하지 않았는가**
  (수정해야 동작한다면 `contract.py`를 확장하고 skill은 선언만으로 되게 고친다)
- 새 probe 종류가 필요하면 `probes.py`에 추가하고 `_DISPATCH`에 등록했는가
