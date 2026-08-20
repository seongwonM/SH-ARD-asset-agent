# CLAUDE.md

CSV 테이블의 컬럼 의미를 해석하고, 그 해석을 데이터로 반증하는 파이프라인.

```bash
make test                  # 전체 테스트 (vLLM 불필요, 가짜 LLM으로 전 구간)
make check                 # 실제 엔드포인트 점검
python run.py ./data.csv   # CSV 하나 해석
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
`None`을 내면 check를 건드리지 않는다).

## skill 추가

`skills/<이름>.md` 파일 하나가 skill 하나다. 파일 내용이 그대로 system 프롬프트가
되고, 등록 절차는 없다(폴더를 읽는다). 절차는 `/new-skill` 슬래시 커맨드 참고.

새 skill을 실제로 돌게 하려면 두 곳만 만진다.

- `pipeline/plan.py` — 언제 도는가(고정 순서 / gap 보충 / 재계획 후보)
- `pipeline/skill_runner.py` — 그 skill이 볼 payload

**payload는 좁게 준다.** 컬럼 단위 skill에 테이블 전체 프로파일을 넣지 않는다 —
컬럼이 늘수록 무관한 정보가 판단을 흐리고 토큰만 커진다. 그룹 단위 skill은
그 그룹에 속한 컬럼의 증거만 본다.

## 작업 시 주의

### 병렬 실행
컬럼별(`column_interpretation`, gap skill)과 관계 그룹별(`semantic_validation`)만
병렬로 돈다. 테이블 단위 skill(`semantic_type`, `relation_analysis`,
`table_context`)은 항상 단독 실행한다 — 같은 결과 슬롯에 둘이 동시에 쓰면
마지막 승자가 비결정적이다. 동시 실행 상한은 `LLM_MAX_CONCURRENCY`(RateLimiter)
하나로 통제한다. 새 병렬 지점을 만들 때 `max_workers`를 따로 정하지 말 것.

### 실행 순서
1차 pass 순서는 `pipeline/plan.py`의 `first_pass_skills()`에 고정돼 있다.
판단 여지가 없는 곳에 LLM 계획 호출을 넣지 말 것. 반대로 컬럼별 보충 판단은
규칙표로 만들지 말 것 — 그건 `gap_planner`에게 맡긴 지점이다.

### LLM 출력을 그대로 실행하지 않는다
계획/배정은 반드시 `plan.py`의 정제 함수를 거친다(모르는 skill 이름, 없는 컬럼,
중복 스텝 제거). 프롬프트가 바뀌어도 실행 계약은 코드가 지킨다.

### 결과 문서 5벌은 계약이다
`columns` / `rulebase` / `plan` / `table` / `llm_calls`. 각각 `<output>.<이름>.json`
으로 떨어지고, 무엇이 어디 들어가는지는 `pipeline/documents.py`에 있다. 배치 로그
수집과 결과 분석이 이 구조를 본다. 바꾸려면 `tests/test_pipeline.py`의 계약
테스트와 `k8s/column-poc-job.yaml`·다운로드 스크립트 주석을 같이 고칠 것.

문서를 가르는 기준은 크기가 아니라 **출처**다. 룰베이스로 계산한 값은 `rulebase`
에만 두고 다른 문서는 `probe_id`/`check_id`로 가리킨다 — 같은 값을 복사해 두면
한쪽만 고쳐질 때 어느 쪽이 맞는지 알 수 없다. 반대로 **LLM에 되돌려주는 피드백
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
- skill 실행 순서를 프롬프트로 정하기 (코드에 있다)
- probe 실행 실패를 반증으로 처리
- 테이블 전체 payload를 컬럼 단위 skill에 통째로 넣기
- k8s 매니페스트의 이미지 태그 수동 수정 (CI가 커밋 SHA로 갱신한다)

## 실험

실험 전용 코드는 `experiments/`에만 둔다(`run_batch.py`, `run_robustness.py`,
`check_endpoint.py`). 제품 경로(`src/`, `run.py`)가 실험 코드를 import하면 안 된다.
브랜치/worktree 운영 규칙은 `EXPERIMENTS.md`.
