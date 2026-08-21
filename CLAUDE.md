# CLAUDE.md

CSV 테이블의 컬럼 의미를 해석하고, 그 해석을 데이터로 반증하는 파이프라인.

```bash
make test                              # 전체 테스트 (vLLM 불필요, 가짜 LLM으로 전 구간)
make check                             # 실제 엔드포인트 점검
make run CSV=./data.csv                # CSV 하나 해석
python -m column_semantics ./data.csv  # 같은 것 (PYTHONPATH=src)
```

전체 구조와 실행 흐름은 `README.md`에 있다. 여기는 **작업할 때 지킬 것**만 적는다.

## 아키텍처 불변식

이 4가지를 깨면 "LLM 없이 검증 가능"과 "실험/운영이 같은 경로를 탄다"가 무너진다.

1. **레이어 의존 방향은 `pipeline -> (core, adapters)` 한 방향이다.**
   `core/`는 pandas와 표준 라이브러리만 안다. core에서 `adapters`나 `os.environ`,
   파일 경로가 등장하면 잘못된 것이다. core 테스트에 가짜 LLM조차 필요 없어야 한다.

2. **LLM 호출은 `adapters/llm.py`에만 있다.**
   파이프라인은 `LLMClient` 프로토콜(`complete_json`)만 본다. 재시도, 레이트리밋,
   JSON 파싱, 타임라인 기록은 전부 어댑터 안이다. pipeline 코드에 `openai`가
   import되면 테스트가 서버를 요구하게 된다.

3. **어댑터를 만드는 곳은 `app.py` 하나다.**
   CLI든 실험 스크립트든 `app.analyze_csv()`를 부른다. 여기가 갈라지면 "로컬에서는
   되는데 배치에서는 다르게 돈다"가 시작된다.

4. **검사 가능한 주장에는 probe를 붙인다.**
   handler의 `if`문으로 검사하지 말 것. skill이 검사식을 내면 `core/probes.py`가
   실제 데이터로 평가하고, **실측값이 그대로 재시도 힌트가 된다.**

## 검증 계층

| 계층 | 위치 | 무엇을 대조하는가 |
|---|---|---|
| 1. 프롬프트 안 자기검사 | `skills/*.md` | 출력 필드 간 정합성 |
| 2. **probe** | `core/probes.py` | **주장 ↔ 실제 데이터** |
| 3. 출력 정제 | `pipeline/plan.py` | LLM이 낸 계획 ↔ 실행 가능한 스텝 |

**2번이 핵심이다.** 1·3번은 결국 텍스트를 텍스트와 대조한다.
"이 컬럼이 primary key다"의 진위는 텍스트가 아니라 데이터에 있고, probe만 그걸 본다.

probe는 반증 도구다. 통과가 참을 증명하지 않는다. 그리고 **probe 실행 실패를
`fail`로 처리하지 말 것** — 평가할 수 없다는 것은 반증이 아니다(`run_probe`가
실측을 못 내면 check의 status를 건드리지 않는다).

**단, 평가하지 못했다는 사실은 남긴다.** `ProbeResult.reason`이 왜 못 쟀는지를
담고, 그대로 `rulebase.probes`에 `not_evaluable`로 들어간다 — skill이 자꾸 없는
컬럼을 가리키는지 표본이 모자란 건지는 프롬프트를 고칠 때 필요한 정보다.

## 고정 단계와 보완 skill

프롬프트 폴더가 둘로 갈려 있고, 기준은 **누가 실행을 결정하는가**다.

| | 폴더 | 실행 결정 | 예 |
|---|---|---|---|
| 고정 단계 | `prompts/` | 코드(`STAGE_ORDER`). 데이터 조건으로만 켜고 끈다 | column_interpretation, relation_analysis, semantic_validation, table_context |
| 보완 skill | `skills/` | domain_gap 게이트 → `gap_planner`를 거쳐 배정 | reconsider_ambiguous, explain_sparsity, reconcile_type_meaning, joint_interpretation |
| 최소 출력 | `prompts/lean_*.md` | 고정 단계마다 짝으로. 기본 켜짐(`--no-lean`으로 끔) | 각 단계의 `lean_*` |

**항상 나와야 하는 산출물을 skill로 만들지 말 것.** 컬럼 해석과 테이블 맥락은
무조건 만들어야 하니 "돌릴지 말지"를 물을 여지가 없다 - 물어보는 순간 비용과
비결정성만 는다. 반대로 컬럼마다 다른 보충은 규칙표로 만들지 말 것 - 그건
gap_planner에게 맡긴 지점이다.

프롬프트 추가는 파일 하나 + 두 곳이다.

- `pipeline/plan.py` — `STAGE_ORDER`(고정 단계) 또는 `GAP_SKILLS`(보완)
- `pipeline/stage_runner.py` — 그 프롬프트가 볼 payload

**payload는 좁게 준다.** 컬럼 단위에 테이블 전체 프로파일을 넣지 않는다 —
컬럼이 늘수록 무관한 정보가 판단을 흐리고 토큰만 커진다. 그룹 단위는
그 그룹에 속한 컬럼의 증거만 본다.

### 보완은 두 단계로 판단하되, 앞쪽은 규칙이다

**누구를 볼지는 코드가 정하고**(해석이 남긴 `domain_gap` 유무), `gap_planner`(단독)가
넘어온 컬럼들을 한자리에서 보고 **"무엇을 할지"**를 정한다. 이 순서를 합치지 말 것.

- 게이트에 LLM을 두지 말 것. 예전에는 `column_review`가 컬럼마다 "더 볼까"를
  물었는데, 물어볼 근거는 해석이 이미 `domain_gap`에 적어둔 것과 같았다 — 같은
  모델에게 같은 재료로 두 번 묻고 두 번째 답을 제어 흐름에 쓰는 구조였다.
  판정이 흔들리고(형식을 못 맞추면 전부 pass) 컬럼 수만큼 호출이 늘었다.
- 게이트에 skill 이름을 고르게 하면 안 된다. 컬럼 하나만 보고서는 "이 둘을 같이
  봐야 한다"를 알 수 없다 — 그 판단이 가능한 곳은 넘어온 컬럼을 함께 보는
  planner 뿐이고, `joint_interpretation`이 거기서만 나오는 이유다.
- **planner가 도는 조건은 게이트 결과다.** 넘어온 컬럼이 0이면 호출하지 않는다.
  임계값으로 게이트를 만들지 말 것.
- **보완은 gap을 닫을 수 있어야 한다.** skill이 `domain_gap: null`을 내면 그대로
  반영된다(다른 필드와 달리 null도 값으로 본다). 닫을 방법이 없으면 한 번 걸린
  컬럼이 라운드마다 다시 걸리고 planner 호출만 는다.

게이트를 넓히고 싶어지면 게이트가 아니라 `column_interpretation`을 고칠 것.
"타입과 의미가 어긋난다"도 결국 그 컬럼을 아직 모른다는 뜻이고, 그걸 domain_gap에
적게 하는 편이 판단 지점을 하나로 유지한다.

### 정제는 실행 가능성만 본다

`sanitize_gap_actions`가 거르는 것은 없는 컬럼, 모르는 행동, 중복, 예산뿐이다.
**근거가 충분한지는 판정하지 않는다** — 그걸 코드가 하려면 semantic_type마다
임계값을 정해야 하고, 그건 LLM이 이미 아는 상식을 실험으로 다시 알아내는 일이다.
planner가 적은 `cites`/`reason`은 검증 없이 `plan.json`에 그대로 싣는다
(나중에 실제 값과 대조할 수 있게). 버린 행동도 이유와 함께 남긴다 — planner가
무엇을 하려 했는지가 사라지면 프롬프트를 고칠 근거도 사라진다.

## 작업 시 주의

### 병렬 실행
컬럼별(`column_interpretation`, 보완 skill)과 관계 그룹별
(`semantic_validation`)만
병렬로 돈다. 테이블 단위 고정 단계(`relation_analysis`, `table_context`)는
항상 단독 실행한다 — 같은 결과 슬롯에 둘이 동시에 쓰면
마지막 승자가 비결정적이다. 동시 실행 상한은 `LLM_MAX_CONCURRENCY`(RateLimiter)
하나로 통제한다. 새 병렬 지점을 만들 때 `max_workers`를 따로 정하지 말 것.

### 실행 순서
1차 pass 순서는 `pipeline/plan.py`의 `first_pass_stages()`에 고정돼 있다.
판단 여지가 없는 곳에 LLM 계획 호출을 넣지 말 것. 반대로 컬럼별 보충 판단은
규칙표로 만들지 말 것 — 그건 `gap_planner`에게 맡긴 지점이다.

### LLM 출력을 그대로 실행하지 않는다
계획/배정은 반드시 `plan.py`의 정제 함수를 거친다(모르는 skill 이름, 없는 컬럼,
중복 스텝 제거). 프롬프트가 바뀌어도 실행 계약은 코드가 지킨다.

### 모르는 것은 문장으로 메우지 않고 필드로 뺀다
프로파일은 컬럼의 모양을 알려주지, 그게 **어느 공정의 무엇인지**는 알려주지 않는다.
그 자리를 "어떤 공정의 측정값" 같은 말로 채우면 답처럼 보이지만 정보가 0이고,
모른다는 사실이 문장 속에 녹아 사라진다. `column_interpretation`은 근거가
받쳐주는 만큼만 `selected_meaning`에 쓰고, 나머지는 `domain_gap`(무엇을 모르는지 /
왜 이 데이터로는 안 되는지 / 무슨 자료가 있으면 풀리는지)으로 뺀다.

**구조적 확정과 도메인 식별은 다른 축이다.** `status: resolved`인데 `domain_gap`이
있는 조합은 정상이고, 오히려 정직한 답이다. 필드로 빠져 있어야 "몇 개 컬럼이
아직 무엇인지 모르는가"를 세어볼 수 있고, `would_resolve`가 모이면 어떤 자료를
구해와야 하는지가 목록이 된다.

### 컬럼 하나에 대한 판단은 한 호출에서 끝낸다
타입(`semantic_type`)과 의미는 같은 컬럼을 같은 근거로 읽는 일이라 `column_interpretation`
한 번이 둘 다 낸다. 예전에는 테이블 단위 `semantic_type` 단계가 따로 있었는데,
그 호출은 **전 컬럼 프로파일**을 payload로 받으면서 정작 프롬프트로는 "다른 컬럼을
근거로 삼지 말라"고 막고 있었다 - 쓰지 말라는 맥락에 컨텍스트를 다 쓰고, 컬럼 수가
늘면 그 한 번의 호출이 먼저 터졌다. 게다가 타입과 의미가 서로 어긋나는 일까지
만들어 그걸 다시 붙이는 보완 skill이 필요했다.

**컬럼 단위 판단을 테이블 단위 호출로 만들지 말 것.** 컬럼 수에 비례해 커지는
payload는 그 자체가 단일 실패 지점이다.

### 프롬프트가 요구한 출력은 반드시 쓰인다
프롬프트에 "이런 필드를 내라"고 적었으면 파이프라인이 그 필드를 실제로 소비해야
한다. `joint_interpretation`에 probe를 내라고 써놓고 실행하지 않은 적이 있는데,
그건 토큰만 쓰고 아무 데도 안 남는 유령 기능이었다. 소비할 자리가 없으면 프롬프트
에서 빼고, 소비하기로 했으면 결과가 어느 문서에 남는지까지 정한다.

### 실행 설정은 meta에 남긴다
예산·라운드 상한처럼 결과를 바꾸는 설정은 모든 문서의 `meta`에 들어간다
(`max_rounds`, `max_gap_rounds`, `max_actions_per_column`, `max_group_columns`).
없으면 결과 두 개를 비교할 때 설정이 바뀐 건지 모델이 다르게 답한 건지 가릴 수 없다.

재시도·타임아웃(`meta.llm_call`)도 같은 이유로 남긴다 - 끝까지 돌았는지가
그 값으로 갈린다. **재시도 정책은 어댑터가 갖는다**(`LLM_MAX_RETRIES` /
`LLM_RETRY_BACKOFF_SECONDS` / `LLM_TIMEOUT_SECONDS` / `LLM_HTTP_RETRIES`).
파이프라인이 단계마다 `max_retries`를 따로 넘기기 시작하면 "이 실행이 몇 번까지
버텼는가"를 한 곳에서 말할 수 없게 된다 - 넘기지 말 것. 오래 도는 배치에서는
백오프가 슬롯을 놓은 뒤에 쉬어야 한다는 점도 지킬 것(붙들고 자면 멀쩡한 다른
컬럼 호출이 그만큼 막힌다).

### 보완 루프에 예산을 지키기
`MAX_GAP_ROUNDS` / `MAX_ACTIONS_PER_COLUMN` / `MAX_GROUP_COLUMNS`는 `plan.py`
한곳에 있다. 라운드를 늘리면 토큰은 확실히 늘지만 해석이 나아진다는 보장은
없으니, 늘리기 전에 `run_robustness.py`의 `flag_ratio`·`dropped_actions`와 회차별
`flagged_columns`(같은 컬럼이 매번 걸리는지)로 효과부터 볼 것. 재검토 대상은 그 라운드에 **실제로 바뀐 컬럼**뿐이다.

### 실험은 k8s Job이 돌린다 - 인자를 바꾸면 같이 고친다
`k8s/column-poc-job.yaml`(Job: `column-poc-batch`)이 **메인 실험 경로**다. CLI
인자나 출력 파일 구조를 바꾸면 이 Job의 셸 스크립트, `k8s/scripts/*.ps1`,
`deploy/Dockerfile`의 COPY 목록이 같이 어긋난다. 셋 다 확인할 것.

`LLM_MODEL`은 쉼표로 여러 모델을 받고, **모델을 도는 것도 결과 폴더 규칙도
CLI(`cli.py`)가 한다.** Job의 셸은 회차(`ITERATIONS`)와 CSV를 순회하며 CLI를
부르는 일만 한다 - 셸에 로직이 쌓이면 테스트할 수가 없다. 실행 하나는 항상
모델 하나이고 결과는 `<타임스탬프>/iter<회차>/<모델명>/<csv이름>/`에 결과 6개와
`run.log`로 떨어진다. 한 폴더에 두 모델 결과가 섞이면 어느 쪽이 낸 건지 파일만
보고는 알 수 없다.

**모델은 접미사가 아니라 폴더 한 겹이다.** 루트에 회차 같은 단계를 더 붙여도
계층이 그대로 유지되게 하려는 것이다 - `<루트>_<모델명>`이었다면 회차가
`iter1_모델명`으로 이름에 눌러붙어 회차별/모델별로 훑을 수가 없다.

눈으로 검토하지 말고 **스크립트를 뽑아서 돌려볼 것.** yaml에서 `- |` 블록을
꺼내 `/data` 경로만 임시 폴더로 바꾸고, `python -m column_semantics` 자리에 결과 파일만 쓰는 스텁을
두면 실제 셸 로직(루프·tee·실패 경로·종료코드)이 그대로 검증된다. macOS에서는
`date -u -d`가 없으니 shim이 필요하다 - 컨테이너(GNU coreutils)에서는 동작한다.

### 결과 문서 6벌은 계약이다
`columns` / `rulebase` / `plan` / `table` / `llm_calls` / `lean`. 각각
`<output>.<이름>.json`으로 떨어지고, 무엇이 어디 들어가는지는
`pipeline/documents.py`에 있다. 배치 로그 수집과 결과 분석이 이 구조를 본다.
바꾸려면 `tests/test_pipeline.py`의 계약 테스트와 `k8s/column-poc-job.yaml`·
다운로드 스크립트 주석을 같이 고칠 것.

`lean`은 켜지 않아도 `enabled: false`로 항상 떨어진다 - 파일 집합이 실행마다
달라지면 "안 켰다"와 "중간에 죽었다"를 파일 목록으로 구분할 수 없다.

문서를 가르는 기준은 크기가 아니라 **출처**다. 룰베이스로 계산한 값은 `rulebase`
에만 두고 다른 문서는 `probe_id`/`check_id`로 가리킨다 — 같은 값을 복사해 두면
한쪽만 고쳐질 때 어느 쪽이 맞는지 알 수 없다.

**새 기능을 넣으면 남길 자리부터 정한다.** 그 값의 성격이 곧 문서다.

| 남기는 것 | 자리 |
|---|---|
| 컬럼 하나가 지나온 판단(검토 판정, 보완으로 인한 변화) | `columns` 단계 이력 |
| 데이터에서 잰 값(probe 실측) | `rulebase`. 다른 곳은 `probe_id`로 |
| 무엇을 왜 돌렸는가(게이트 결과, 계획, 버린 행동) | `plan` |
| 컬럼 하나에 속하지 않는 산출물(테이블 설명, 관계, 묶어 본 결과) | `table` |
| 호출 원문 | `llm_calls`. 자동으로 들어가니 따로 복사하지 말 것 |

컬럼 여러 개를 묶어 본 결과가 대표적인 예다. 관계는 컬럼 하나의 값이 아니라
`table.joint_findings`에 한 벌만 두고, 컬럼 이력은 `with_columns`로 그 묶음을
가리킨다 — 컬럼마다 복사하면 그룹 크기만큼 같은 문장이 늘어난다. 반대로 **LLM에 되돌려주는 피드백
에는 실측값을 `measured`로 붙인다**(`core/probes.py`의 `with_measurements`).
저장 구조 때문에 재시도 힌트가 비면 probe를 붙인 의미가 없어진다.

### 덮어쓰기는 기록하고 덮어쓴다
gap 보충과 수정 라운드는 `column_interpretation.columns[col]`을 제자리에서
갈아치운다. 그 전에 `ColumnHistory`에 before/after를 남기지 않으면 "무엇이 왜
바뀌었는지"가 사라진다 - 새로 값을 덮어쓰는 지점을 만들면 기록도 같이 만들 것.

### 프로파일러에서 추정값을 만들지 않는다
`core/profiling.py`의 출력이 이후 모든 판단의 근거 집합이라, 오염되면 전파된다.
판정이 서지 않으면 값을 지어내는 대신 `None`을 낸다.

## 하지 말 것

- `core/`에서 환경변수/파일/네트워크 접근
- pipeline에서 `openai` import
- 고정 단계 실행 순서를 프롬프트로 정하기 (코드에 있다)
- 항상 도는 산출물을 `skills/`에 두기 (거긴 컬럼별 보완 전용이다)
- probe 실행 실패를 반증으로 처리
- 테이블 전체 payload를 컬럼 단위 프롬프트에 통째로 넣기
- k8s 매니페스트의 이미지 태그 수동 수정 (CI가 커밋 SHA로 갱신한다)

## 실험

실험 전용 코드는 `experiments/`에만 둔다(`run_batch.py`, `run_robustness.py`,
`check_endpoint.py`). 제품 경로(`src/`)가 실험 코드를 import하면 안 된다.
브랜치/worktree 운영 규칙은 `EXPERIMENTS.md`.
