# asset-context-agent

CSV 테이블을 받아 **컬럼이 무엇을 뜻하는지** 해석하고, 그 해석을 **데이터로 반증**한다.

```bash
make install
make test                      # vLLM 없이 전 구간 (가짜 LLM, 41 tests)
cp .env.example .env           # LLM_API_ENDPOINT / LLM_MODEL 채우기
make check                     # 엔드포인트 점검
python -m column_semantics ./data.csv --output result.json
```

## 무엇이 다른가

LLM에게 컬럼 의미를 물으면 그럴듯한 답이 온다. 문제는 **그게 맞는지 알 방법이
없다**는 것이다. 이 파이프라인은 두 가지로 그 문제를 좁힌다.

1. **LLM은 raw 데이터를 보지 않는다.** 먼저 결정론적으로 프로파일(유일성, 결측,
   분포, 컬럼 쌍 관계, 복합키 후보)을 계산하고, LLM은 그 사실만 본다.
2. **주장은 데이터에 대고 반증한다.** 검증 skill이 "power_value는 power_limit
   이하다" 같은 주장을 낼 때 검사식(`probe`)을 같이 내고, 그 식을 실제
   DataFrame에 평가한다. LLM이 `pass`라고 써도 실측이 어긋나면 `fail`이 된다.

probe는 반증 도구다. 통과가 참을 증명하지 않고, 실패가 거짓을 증명한다.
그리고 **실행 실패는 반증이 아니다** — 컬럼이 없거나 식이 깨졌으면 주장을
건드리지 않고 그대로 둔다.

## 구조

```
src/column_semantics/      진입점은 `python -m column_semantics`
  core/                    DataFrame -> 사실. 순수 계산, LLM/파일/환경변수를 모른다
    profiling.py             컬럼별 프로파일
    relations.py             컬럼 쌍 관계 / 복합키 후보 / 관계 그룹
    evidence.py              위 둘을 묶은 증거 블록
    probes.py                skill이 요청한 검사식 평가 (안전한 산술/비교 계산기)
    history.py               컬럼이 단계마다 어떻게 바뀌었는지
    llm_log.py               LLM 호출 입출력 원문
    timeline.py, clock.py    실행 궤적
  adapters/                바깥 세계. 여기만 갈아끼우면 다른 환경에 붙는다
    csv_source.py            인코딩 추정 + 깨진 행 복구 진입점
    csv_repair.py            값 안의 쉼표로 쪼개진 행 되돌리기 (컬럼 프로파일 + DP)
    llm.py                   OpenAI 호환 엔드포인트 (LLMClient 프로토콜)
    prompts.py               prompts/*.md, skills/*.md 로딩
    env.py, ratelimit.py
  pipeline/                조립. 순서와 병렬화
    plan.py                  고정 단계 순서 + LLM 계획 출력 정제
    stage_runner.py          단계/skill별 payload 조립
    orchestrator.py          실행 루프
    documents.py             결과를 6개 문서로 나눠 담기
  app.py                   composition root (CLI도 실험도 여기를 부른다)
  cli.py                   인자 파싱 + 모델별 반복 + 결과 폴더/로그 배치
  __main__.py              `python -m column_semantics`
prompts/*.md               고정 단계 프롬프트. 코드가 순서대로 돌린다
skills/*.md                보완 skill. domain_gap이 남은 컬럼에만 붙는다
experiments/               실험 전용. 제품 경로가 아니다
tools/                     운영용 CLI. 로직은 없고 src를 부른다(깨진 CSV 진단 등)
k8s/                       배치 Job / 디버그 Pod / PVC
```

의존 방향은 `pipeline -> (core, adapters)` 한 방향이다. core가 adapters를
import하면 "LLM 없이 프로파일링만 검증"이 불가능해진다.

## 실행 흐름

실행 단위는 두 종류고, **누가 실행을 결정하는가**로 갈린다.

```
[고정 단계 · prompts/]  코드가 정한 순서. 무조건 만들어야 하는 산출물이다
1차    column_interpretation(컬럼별 병렬 — 타입과 의미를 한 번에)

[게이트 · 코드]  LLM 호출이 아니라 규칙이다
gate   해석이 `domain_gap`을 남긴 컬럼만 보완으로 넘긴다

[보완 skill · skills/]  게이트가 넘긴 컬럼에만 붙는다
gap    넘어온 컬럼 있음 → gap_planner가 무엇을 할지 결정 → 배정된 skill 병렬 실행
       → 바뀐 컬럼만 다시 판정 (최대 2라운드)

[고정 단계]
관계   relation_analysis*
검증   관계 그룹별 semantic_validation 병렬 → probe로 실측 대조
마무리 table_context

수정   needs_revision이면 planner가 재계획 → 해당 고정 단계만 재실행 → 재검증
```

`*` pairwise 증거가 하나도 없으면 relation_analysis는 호출하지 않는다 — 이것도
LLM 판단이 아니라 데이터 조건이다.

**고정 단계는 LLM에게 "돌릴까요"를 묻지 않는다.** 컬럼 해석도 테이블 맥락도
반드시 나와야 하는 산출물이라 물어볼 여지가 없고, 판단할 여지가 없는 곳에 계획
호출을 넣으면 비용만 늘고 재현성이 떨어진다. LLM이 계획하는 지점은 두 곳뿐이다
— 컬럼별 보완 배정(`gap_planner`)과 검증 실패 후 재계획(`planner`, 고정 단계
중에서만 고른다).

**누구를 보완할지도 묻지 않는다.** 예전에는 `column_review`가 컬럼마다 "더
볼까요"를 물었는데, 물어볼 근거는 해석이 이미 `domain_gap`에 적어둔 것과 같았다 —
같은 모델에게 같은 재료로 두 번 묻고 두 번째 답을 제어 흐름에 쓰는 구조였다.
지금은 필드 하나를 보는 규칙이라 공짜이고, 판정이 흔들리지 않는다. 대신
domain_gap이 없는 컬럼은 다른 문제가 있어도 보완을 받지 못한다 — 그 컬럼도
보고 싶다면 게이트를 넓힐 게 아니라 해석이 그 상태를 domain_gap으로 남기게
해야 한다.

## 결과 파일

`--output result.semantic.json`을 주면 그 경로를 기준으로 **문서 6개**가 나온다.
가르는 기준은 크기가 아니라 **출처**다 — LLM이 주장한 것, 데이터가 측정한 것,
코드가 계획한 것이 한 트리에 섞여 있으면 결과를 분석할 때마다 어느 쪽 근거인지
다시 따져야 한다.

| 파일 | 담는 것 |
|---|---|
| `<out>.columns.json` | 컬럼별 해석이 단계마다 어떻게 바뀌었는지 (`stages`의 before/after/changed). 도메인을 못 밝힌 컬럼은 `domain_gap`에 무엇이 모자란지가 남는다 |
| `<out>.rulebase.json` | 룰베이스 계산값 전부 — 프로파일, 관계 증거, grain 후보, probe 실측값 |
| `<out>.plan.json` | 1차 고정 순서, gap 배정, 재계획 라운드(LLM 원출력 + 정제 결과), 실행 구간 |
| `<out>.table.json` | 테이블 단위 산출물 — table_context, relation_analysis, 검증 라운드 |
| `<out>.llm_calls.json` | 모든 LLM 호출의 system 프롬프트 / 입력 payload / 응답 원문 |
| `<out>.lean.json` | `--lean`일 때만 채워진다 — 같은 payload로 따로 받아둔 **최소 출력** |

모든 문서가 같은 `meta`(status, validation_status, llm_model, started_at, …)를
들고 있고 `meta.part`로 자기가 어느 문서인지 밝힌다.

### 최소 출력 트랙 (`--lean`)

운영에서 실제로 쓰는 것은 **컬럼 의미**와 **테이블 의미** 두 축인데, 지금 단계들의
출력에는 분석하려고 붙인 필드가 훨씬 많다(후보 목록, 근거/반대근거, confidence,
대안 타입…). 그걸 빼도 의미가 그대로 나오는지 알아야 프롬프트를 줄일 수 있다.

```bash
python -m column_semantics ./data.csv --lean     # 또는 LEAN_TRACK=1
```

고정 단계마다 짝이 되는 `lean_*` 프롬프트를 **같은 payload로 한 번 더** 부르고,
결과를 `lean` 문서에만 남긴다. 한 호출에 둘 다 내게 하지 않는 이유는 긴 출력이
짧은 출력을 끌고 가서 비교가 성립하지 않기 때문이고, 별도 문서인 이유는 이게
산출물이 아니라 **산출물에 대한 측정**이기 때문이다 — **파이프라인은 이 값을
읽지 않는다.** LLM 호출은 대략 두 배가 되므로 기본은 꺼져 있다.

`python tools/report_md.py <경로>`가 두 출력을 한 줄에 놓고 보여준다.

**측정값은 rulebase에만 있다.** check는 `probe_id`로, 컬럼 이력은 `check_id`로
가리킨다 — 같은 값을 두 문서에 복사해 두면 한쪽만 고쳐질 때 어느 쪽이 맞는지
알 수 없다. 단, LLM에게 되돌려주는 재시도 피드백에는 실측값을 `measured`로 붙여
보낸다. 반증의 근거가 곧 다음 시도의 힌트다.

읽을 때는 그 id를 사람이 손으로 이어야 하므로, 조인까지 끝낸 Markdown 한 장을
따로 뽑는다. 새 값은 만들지 않고 문서에 있는 것만 옮긴다.

```bash
python tools/report_md.py ./results/20260820_1200_modelA/my_table  # 그 폴더에 report.md
python tools/report_md.py ./results --all                          # 실행마다 report.md + index.md
python tools/report_md.py <경로> --include-calls                   # LLM 호출 원문까지
python tools/report_md.py <경로> --no-detail                       # 컬럼별 상세 없이 요약만

make report DIR=./results/20260820_1200_modelA/my_table            # make를 쓴다면
```

표준 라이브러리만 쓰므로 PYTHONPATH도, 설치도 필요 없다. 폴더 대신 기준 경로나
문서 파일 하나를 줘도 나머지를 알아서 찾는다.

단계가 끝날 때마다 이 6개 파일을 그대로 덮어쓴다. 중간에 죽어도 그때까지의
결과는 파일에 남고, 완주 여부는 `meta.status`(`in_progress` / `done`)로 본다.

`meta.validation_status`가 `unresolved_after_max_rounds`면 반증된 주장을
끝내 해소하지 못한 채 끝난 것이다. `done`만 보고 넘어가면 안 된다.

## 프롬프트 추가

파일을 어느 폴더에 두느냐가 곧 "언제 도는가"다.

- **보완 skill** — `skills/<이름>.md` + `plan.py`의 `GAP_SKILLS`에 이름 추가.
  gap_planner가 배정할 때만 붙는다. 여러 컬럼을 한 번에 보는 skill이면
  `MULTI_COLUMN_SKILLS`에도 넣는다.
- **고정 단계** — `prompts/<이름>.md` + `plan.py`의 `STAGE_ORDER`에 순서 지정.
  항상(또는 데이터 조건에 따라) 돈다. 정말 매번 필요한 산출물인지 먼저 따져볼 것.

등록 절차는 없다 — 폴더를 읽는다. 자세한 절차는 `.claude/commands/new-skill.md`.

## 배치 / k8s

```bash
# 로컬: 폴더 안 CSV 전부
make batch DATA=./data OUT=./results

# k8s: 이미지 안 코드로 바로 돈다 (CI가 커밋 SHA로 이미지 태그를 갱신)
./k8s/scripts/create-secret-from-env.ps1        # .env의 LLM_* -> k8s secret
kubectl apply -f k8s/data-pvc.yaml
./k8s/scripts/upload-robustness-data.ps1        # data/robustness-test의 CSV+metadata -> PVC
kubectl delete job column-poc-batch --ignore-not-found   # Job spec은 불변이다
kubectl apply -f k8s/column-poc-job.yaml
kubectl logs -f job/column-poc-batch                     # 첫 줄 [SRC]에 빌드 SHA
./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results
```

데이터 업로드는 **기존 내용을 지우고 새로 넣는다**. Job은 `/data/robustness_test`의
`*.csv`를 전부 도니까, 덧씌우기만 하면 예전 실험 CSV가 남아 같이 돌아간다 —
결과 폴더에 낯선 이름이 하나 더 있는 것으로만 드러나서 알아채기 어렵다.
지우지 않고 얹으려면 `-KeepExisting`.

결과는 `<KST타임스탬프>/iter<회차>/<모델명>/<csv_stem>/`에 문서 6개와 `run.log`로
떨어진다. **`LLM_MODEL`에 쉼표로 여러 모델을 적으면 모델마다 한 바퀴씩 돌고 폴더가
갈린다** — 타임스탬프는 같으니 같은 실험으로 묶인다. Job의 `ITERATIONS`는 그
한 바퀴를 통째로 몇 번 반복할지다(강건성 테스트: 같은 입력에 답이 얼마나
흔들리는지). 1이어도 `iter1/`은 생긴다 — 경로 모양이 반복 여부에 따라 달라지면
결과를 읽는 쪽이 두 가지를 알아야 한다. 중간에 죽은 CSV도 파일은 남으므로,
완주 여부는 파일 유무가 아니라 `meta.status`로 본다.

실행 조건(모델·엔드포인트·RPM·동시성·예산·행/컬럼 수)은 시작 시 `[BATCH]`/`[CONFIG]`
줄로 로그에 찍히고, 같은 값이 모든 결과 문서의 `meta`에도 들어간다.

이미지를 다시 굽지 않고 코드만 바꿔 돌려보려면
`./k8s/scripts/upload-column-poc.ps1`로 PVC에 올린다. Job은 PVC에 코드가 있으면
그쪽을 쓴다 — **실험이 끝나면 반드시 지울 것.** 안 지우면 낡은 코드가 이후 모든
Job에서 조용히 계속 쓰인다.
