# CLAUDE.md

테이블을 받아 컬럼을 해석하고 asset context를 만드는 에이전트.
**고정 파이프라인이 아니다.** 현재 상태를 보고 다음에 필요한 skill을 매번 다시 고른다.

```bash
make test    # 전체 테스트 (vLLM 불필요, mock으로 실행)
make trace   # 실행 궤적 출력
```

## 아키텍처 불변식

이 5가지를 깨는 변경은 하지 말 것. 어기면 확장성이 사라진다.

1. **그래프 노드는 `plan`과 `act` 둘뿐이다.**
   작업 종류가 늘어도 노드를 추가하지 않는다. 그래프에 skill 이름이 등장하면 설계가 무너진 것.

2. **skill 추가 = `skills/<name>/` 폴더 추가.**
   `planner.py`, `graph.py`, `executor.py`는 skill이 몇 개든 수정하지 않는다.
   이 세 파일을 고쳐야 skill이 동작한다면 계약(`contract.py`)이 부족한 것이므로
   계약을 확장하고 skill은 선언만으로 되게 만든다.

3. **`board`가 유일한 저장소다.**
   컬럼 사실, 중간 결과, 최종 산출물 모두 board에 있다. state에 별도 필드를 두지 않는다.
   (초기 구현에서 `column_facts`를 state에 따로 뒀다가 board와 어긋나 gap 계산이 전면 실패했다.)

4. **검사 가능한 주장에는 probe를 붙인다.**
   handler 안 `if`문으로 검사하지 말 것. `VerifiableClaim`으로 선언하면 executor가
   데이터로 실행하고, **실측값이 그대로 재시도 힌트가 된다.**
   `if`문은 그 힌트를 사람이 직접 써야 하고, 대개 "틀렸다"까지만 쓰게 된다.

5. **skill은 실패해도 루프를 죽이지 않는다.**
   실패한 `(skill, target)` 조합만 `blocked`에 격리하고 플래너가 다른 경로를 찾는다.

## 3단 검증 구조

| 계층 | 위치 | 무엇을 대조하는가 | 예시 |
|---|---|---|---|
| 1. skill 자체 | 각 `handler.py` | 출력 필드 간 정합성 | `unit`이 있는데 `unit_evidence=not_found` |
| 2. **probe** | `probes.py` + executor | **주장 ↔ 실제 데이터** | "primary key다" → 유일성 0.5 |
| 3. 공통 가드 | `guards.py` | 텍스트 ↔ 입력 텍스트 | 입력에 없는 수치 `495` |

**2번이 핵심이다.** 1·3번은 결국 텍스트를 텍스트와 대조한다.
"이 컬럼이 primary key다"의 진위는 텍스트가 아니라 데이터에 있고, probe만 그걸 볼 수 있다.

probe는 **반증** 도구다. 통과가 참을 증명하지 않고, 실패가 거짓을 증명한다.
그래서 `verify-context`는 "전부 맞다"고 말하지 않고 `verified`/`refuted`/`unverified`를 구분해서 남긴다.
`unverified` 목록이 사람이 봐야 할 큐다.

## 새 skill 추가

`/new-skill <이름> <용도>` 슬래시 커맨드를 쓰거나, `skills/_template/`을 복사한다.

```
skills/<snake_name>/
  SKILL.md     필수. YAML frontmatter + 본문(LLM 지침)
  handler.py   필수. async def run(ctx, deps) -> SkillResult
```

등록 절차는 없다 — registry가 폴더를 스캔한다.

| frontmatter | 의미 |
|---|---|
| `description` | **플래너가 보는 유일한 설명.** "무엇을 하는가"보다 "언제 골라야 하는가" |
| `requires` | 선행 슬롯. **완결**되어야 후보가 된다 (부분 충족 아님) |
| `provides` | 채우는 슬롯 |
| `applies_when` | `column_kinds`, `min_rows`, `column_name_patterns`, `always` |
| `per_column` | true면 컬럼 단위로 반복 실행 → 병렬 배치 대상 |
| `cost` | free/low/medium/high. 점수에서 감점 |

**실행 순서는 `requires`/`provides`로만 표현한다.** 점수로 순서를 흉내내지 말 것.
(실제로 `synthesize-context`가 `grain`을 requires에 안 넣었더니 점수가 높아서 먼저 실행돼
입도 판정을 건너뛰었다.)

본문은 선택된 뒤에야 로드되어 handler의 system 프롬프트가 된다(progressive disclosure).
그래서 본문이 길어도 플래닝 비용은 늘지 않는다.

## 작업 시 주의

### Pydantic 모델 (vLLM guided decoding 제약)
- `Optional`/`Union` 금지 → "값 없음"은 빈 문자열
- 라벨은 `Literal`로 고정 → 시맨틱 드리프트를 디코딩 단계에서 차단
- 중첩 2단계 이내, `extra="forbid"`
- handler 안에서 모델을 정의할 때 registry가 `sys.modules`에 등록해 주므로
  `from __future__ import annotations`를 그대로 써도 된다 (등록을 빼면 전부 실패한다)

### 병렬 배치
`planner.build_batch()`는 **keyed 슬롯에만 쓰는 per_column 작업**만 묶는다.
단일 값 슬롯(`grain`, `summary` 등)에 쓰는 작업은 항상 단독 실행한다 —
두 skill이 같은 슬롯에 동시에 쓰면 마지막 승자가 비결정적이다.
새 슬롯을 만들 때 대상별로 채워진다면 `planner.KEYED_SLOTS`에 추가한다.

### 가드레일
`guards.py`는 `NL_KEYS`에 있는 키의 값만 검사한다.
자연어 필드를 추가하면 `NL_KEYS`에 넣어야 검사 대상이 된다.
반대로 제어용 라벨(`unit_evidence` 등)은 절대 넣지 않는다 —
넣으면 정상 값이 오탐되어 전부 실패하고, 결과적으로 가드를 끈 것과 같아진다.

### probe detail 작성
`detail`에 **실측값을 반드시 포함**한다. 이 문자열이 그대로 LLM 재시도 힌트가 된다.
- 나쁨: "유일하지 않다"
- 좋음: "실제 유일성 비율은 0.42이다(기준 0.99). 중복 예시: [...]"

## 하지 말 것

- 그래프에 skill별 노드 추가
- `planner.py`에 skill 이름 하드코딩
- skill 실행 순서를 코드로 고정
- 프로파일러에서 추정값 생성 — 그 출력이 모든 가드의 근거 집합이라 오염이 전파된다
- 가드에서 모든 문자열 무차별 검사
- probe 실행 실패를 `passed=False`로 처리 — 실행 실패는 반증이 아니다

## 기존 레포 연동

`MIGRATION.md` 참조. 핵심 제약 하나만 여기 옮긴다.

**`src/agent/compat.py`의 출력 키를 바꾸지 말 것.**
기존 `run_robustness_test.py`(최대 1440회 배치)와 `analyze_robustness_test.py`가
이 모양에 결합돼 있고, 결과는 PVC에 누적된다. 키를 바꾸면 옛 결과와
새 결과를 비교할 수 없어 엔진 교체 효과를 측정할 수 없다.
신규 정보는 **추가만** 한다(`asset_context.verification`, `trace`).

`tests/test_legacy_contract.py`가 이 계약을 지킨다. 실패하면 compat을 고치고,
배치 스크립트는 건드리지 않는다.

## 남은 작업

`HANDOFF.md`에 우선순위와 함께 정리되어 있다. 가장 급한 것:

1. `src/agent/llm.py: RuntimeDeps._post()` — 실제 vLLM 호출부 (현재 `NotImplementedError`)
2. `VLLM_STRUCTURED_MODE` 확정 — 서빙 버전 문서 확인
3. 실제 테이블로 `profile_table/handler.py`의 kind 판정 임계값 검증
