---
description: 새 프롬프트(보완 skill / 고정 단계)를 추가하고 파이프라인에 연결한다
argument-hint: <name> [무엇을 판단하는가]
---

`$1`을 추가한다. 대상: $2

프롬프트는 코드가 아니라 파일이다. 파일 하나(`<폴더>/<name>.md`)가 프롬프트
하나이고, 그 내용이 그대로 LLM system 프롬프트가 된다. 등록 절차는 없다 —
폴더를 읽는다.

## 먼저 정할 것: 어느 폴더인가

폴더가 곧 "언제 도는가"다. 여기서 틀리면 나머지가 다 어긋난다.

| | 폴더 | 언제 도는가 | 이럴 때 고른다 |
|---|---|---|---|
| 보완 skill | `skills/` | column_review가 넘긴 컬럼에 gap_planner가 배정할 때만 | 컬럼마다 필요 여부가 다르다 |
| 고정 단계 | `prompts/` | 코드가 정한 순서대로 항상 | 무조건 나와야 하는 산출물이다 |

**기본은 `skills/`다.** 고정 단계로 만들려면 "이 산출물이 모든 테이블에서 매번
필요한가"에 답할 수 있어야 한다 — 아니라면 컬럼별 보완이 맞다.

## 절차

1. 파일을 만든다. 기존 것(`prompts/column_interpretation.md`,
   `skills/reconsider_ambiguous.md`)의 형식을 따른다: Role / 입력 설명 /
   판단 규칙 / **금지** / 출력 JSON 스키마.
2. 본문에 **금지 항목**을 구체적으로 적는다. 이 프롬프트가 근거 없이 만들어내기
   쉬운 것이 무엇인지 생각해서 적는다("표본에 없는 값을 예시로 들지 마라",
   "단위를 지어내지 마라").
3. 룰베이스가 이미 계산한 값을 다시 도출하라고 시키지 말 것. 프로파일에 있는
   값은 근거로 쓰게 하고, 없는 값은 애초에 물어보지 않는다.
4. 출력에 **데이터로 확인 가능한 주장**이 있으면 검사식(`probe`)을 같이 내게
   한다. `{"expression": "v <= lim", "columns": {"v": "...", "lim": "..."}}`
   형식이고, `core/probes.py`가 실제 DataFrame에 평가한다.
   프롬프트에 "스스로 검산하라"고 쓰지 말 것 — 그건 검증이 아니다.
5. `src/column_semantics/pipeline/plan.py`에 선언한다.
   - 보완 skill이면 `GAP_SKILLS`에 이름 추가 (gap_planner가 고른다).
     여러 컬럼을 한 번에 보는 skill이면 `MULTI_COLUMN_SKILLS`에도 넣는다 -
     정제가 컬럼 개수를 그걸로 검사한다
   - 고정 단계면 `STAGE_ORDER`에 순서 지정 (+ 1차에 넣을 거면 `first_pass_stages`)
   - `REQUIRED_SKILLS` / `REQUIRED_PROMPTS`에 들어가면 파일이 없을 때 즉시 실패한다
6. `prompts/gap_planner.md`의 행동 목록에 한 줄 넣는다. 프롬프트에 없으면
   planner가 고를 수 없고, 이름만 코드에 있으면 영영 안 불린다.
7. `src/column_semantics/pipeline/stage_runner.py`에 payload 조립을 추가한다.
   **필요한 것만 넣는다.** 컬럼 단위면 그 컬럼 프로파일만, 그룹 단위면 그 그룹
   범위로 자른 증거만. 호출은 보완이면 `_call_skill`, 고정 단계면 `_call_stage`로
   한다 — 그래야 llm_calls 문서에 `kind`가 제대로 남는다.
8. `tests/fakes.py`의 `FakeLLM`에 `_on_<label앞부분>` 응답을 추가하고
   `make test`로 검증한다.

## 확인할 것

- `core/`를 고치지 않았는가 (프롬프트 추가로 core가 바뀔 일은 없다)
- payload에 테이블 전체를 통째로 넣지 않았는가
- 컬럼 상태를 덮어쓰는 보완이면 `ColumnHistory`에 before/after가 남는가
- 컬럼별 병렬 실행 대상이면 결과가 컬럼별 슬롯에만 쓰이는가
  (테이블 단위 슬롯에 병렬로 쓰면 마지막 승자가 비결정적이다)
