---
description: 새 skill(프롬프트)을 추가하고 파이프라인에 연결한다
argument-hint: <skill-name> [무엇을 판단하는 skill인가]
---

`$1` skill을 추가한다. 대상: $2

skill은 코드가 아니라 프롬프트다. 파일 하나(`skills/<name>.md`)가 skill 하나이고,
그 내용이 그대로 LLM system 프롬프트가 된다. 등록 절차는 없다 — 폴더를 읽는다.

## 절차

1. `skills/$1.md`를 만든다. 기존 skill(`column_interpretation.md`,
   `semantic_validation.md`)의 형식을 따른다: Role / 입력 설명 / 판단 규칙 /
   **금지** / 출력 JSON 스키마.
2. 본문에 **금지 항목**을 구체적으로 적는다. 이 skill이 근거 없이 만들어내기
   쉬운 것이 무엇인지 생각해서 적는다("표본에 없는 값을 예시로 들지 마라",
   "단위를 지어내지 마라").
3. 출력에 **데이터로 확인 가능한 주장**이 있으면 검사식(`probe`)을 같이 내게
   한다. `{"expression": "v <= lim", "columns": {"v": "...", "lim": "..."}}`
   형식이고, `core/probes.py`가 실제 DataFrame에 평가한다.
   프롬프트에 "스스로 검산하라"고 쓰지 말 것 — 그건 검증이 아니다.
4. `src/column_semantics/pipeline/plan.py`에 언제 도는지 선언한다.
   - 1차 고정 순서에 넣을 것인가(`SKILL_ORDER` + `first_pass_skills`)
   - 컬럼별 보충인가(`GAP_SKILLS` — gap_planner가 고른다)
   - `REQUIRED_SKILLS`에 들어가면 파일이 없을 때 즉시 실패한다
5. `src/column_semantics/pipeline/skill_runner.py`에 payload 조립을 추가한다.
   **필요한 것만 넣는다.** 컬럼 단위면 그 컬럼 프로파일만, 그룹 단위면 그 그룹
   범위로 자른 증거만.
6. `tests/fakes.py`의 `FakeLLM`에 `_on_<label앞부분>` 응답을 추가하고
   `make test`로 검증한다.

## 확인할 것

- `core/`를 고치지 않았는가 (skill 추가로 core가 바뀔 일은 없다)
- payload에 테이블 전체를 통째로 넣지 않았는가
- 컬럼별 병렬 실행 대상이면 결과가 컬럼별 슬롯에만 쓰이는가
  (테이블 단위 슬롯에 병렬로 쓰면 마지막 승자가 비결정적이다)
