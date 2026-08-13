# 시스템 설계 구조 (2026-08-13 기준, `exp/evidence-first-redesign`)

`ARCHITECTURE.md`가 에이전트의 **인지 설계**(evidence-first, plan/act 루프, skill role)를
다룬다면, 이 문서는 그 위/아래에 있는 **시스템 전체 구조**를 다룬다: 코드가 어떻게
나뉘어 있는지, LLM 호출이 실제로 어떻게 되는지, k8s에 어떻게 떠 있는지, CI가 뭘
자동으로 하는지, 그리고 아직 안 풀린 문제가 뭔지.

## 1. 코드 레이아웃

```
src/agent/            에이전트 코어 (패키지명 agent, sys.path로 src/를 루트에 추가해서 import)
  contract.py            Slot enum, SkillManifest, SkillResult 등 — 유일한 확장 지점
  state.py               Board(blackboard) + AgentState(LangGraph 상태)
  planner.py             gap 계산 → 후보 스코어링 → 배치 구성
  executor.py            배치 동시 실행 + 3단 검증(self/probe/guard)
  graph.py               LangGraph 조립 (plan → act → plan 루프, 노드 2개)
  guards.py              텍스트 근거 대조 가드
  probes.py              데이터 기반 반증 (VerifiableClaim의 실행기)
  registry.py            skills/*/SKILL.md 로딩·인덱싱
  skill_utils.py          skill handler 공통 유틸
  llm.py                  RuntimeDeps — OpenAI 호환 엔드포인트 호출, RPM/동시성/재시도, 통계
  config.py               .env 파싱, 다중 모델(MODEL1/2/3) 해석, 반복횟수 등 하이퍼파라미터
  exp_logging.py          exp{N} 폴더 생성 + 로거 세팅 + 시작/종료 배너
  csv_repair.py           쉼표로 깨진 CSV 자동 복구 (독립 유틸, 다른 모듈에 의존 안 함)

skills/<name>/          선언형 skill. SKILL.md(frontmatter+본문) + handler.py
  _template/               새 skill 만들 때 복사하는 뼈대
  profile_table            observer — 컬럼 구조/통계 (rule-based, LLM 없음)
  generic_column, numeric_measure, categorical_code, temporal_axis,
  identifier_link          interpreter — 컬럼 종류별 의미 해석
  distribution_profile, value_pattern_profile, join_key_analysis,
  dependency_check, pii_detection, quality_risk_assessment,
  glossary_align            observer — 결정론적/구조화 evidence 계산
  analysis_planning         deliberator — evidence를 보고 다음 작업 계획
  grain_resolution           interpreter — 행 단위(grain) 판정
  verify_context             verifier — probe 교차 검증
  synthesize_context         synthesizer — 최종 asset_context 합성

examples/               외부 진입점 (CLI)
  run_local.py             CSV 1건 실행 (asset-run-job.yaml이 씀)
  run_robustness_test.py   여러 CSV x 여러 모델 x N회 반복 배치 (robustness-job.yaml이 씀)
  make_mock_data.py        정답지 포함 mock 데이터 생성

bench/                  정답 대비 정확도 측정
  run_bench.py             mock truth 기준 벤치 (run_robustness_test.py와 같은 축)
  scoring.py                score_against_truth / score_process / consistency
  offline_llm.py            OfflineDeps — LLM 없이 도는 결정론적 스텁
  check_endpoint.py         structured output 모드가 실제로 강제되는지 점검

k8s/                    배포 매니페스트 + 운영 스크립트
tests/                  pytest, 전부 offline stub 기반이라 LLM 키 불필요
```

## 2. 실행 흐름 요약

`ARCHITECTURE.md`가 상세히 다루므로 여기서는 시스템 관점 요약만.

```
CSV + metadata.json
   │
   ▼
TableAssetContextRunner.build()          (runner.py)
   │  new_state() 로 AgentState 초기화
   ▼
LangGraph: plan ⇄ act 루프                (graph.py)
   │
   │  plan_node: planner.decide()
   │    1) compute_gaps()      board에서 아직 안 채워진 Slot 계산
   │    2) score_candidates()  requires 충족 + applies_when 매칭 skill에 점수
   │    3) build_batch()       쓰기 좌표 안 겹치는 것들 최대 8개(MAX_BATCH) 묶음
   │
   │  act_node: executor.execute_batch()
   │    asyncio.gather로 배치 동시 실행
   │    각 작업: skill.handler() 호출 → 1단 자체검증 → 2단 probe 검증 → 3단 guard
   │    실패하면 (skill,target)을 blocked에 격리하고 계속 (전체를 안 죽임)
   │
   ▼  done=True (goal_reached / iteration_budget_exhausted /
   │             llm_budget_exhausted / no_applicable_skill)
   ▼
_to_result()                              board → column_analysis/data_interpretation/
                                           asset_context/issues/performance/trace 로 변환
```

**핵심 설계**: 파이프라인이 아니라 blackboard다. `Board`(`values`/`keyed`/`artifacts`)가
유일한 상태 저장소이고, skill은 "무엇을 요구하고(requires) 무엇을 채우는지(provides)"만
선언한다. 순서는 planner가 매 iteration마다 gap 기반으로 다시 정한다 — 그래서 skill을
추가해도 그래프(`graph.py`)나 플래너 코드를 안 건드려도 된다.

## 3. LLM 계층 (`src/agent/llm.py`)

`RuntimeDeps` 하나가 이 시스템의 유일한 LLM 호출 경로다(`SkillDeps.structured()` 구현체).

| 관심사 | 구현 |
|---|---|
| structured output 강제 | `LLM_STRUCTURED_MODE`: `guided_json`(vLLM 구형) / `json_schema`(vLLM 신형·OpenAI) / `prompt`(강제 없음, 파싱 재시도) |
| RPM 제한 | `_Throttle` — 호출 "보내는 시점" 간격만 강제. 배치 전체가 하나의 `RuntimeDeps`(=하나의 스로틀 상태)를 공유해야 한도가 지켜짐 |
| 동시성 제한 | `asyncio.Semaphore(MAX_CONCURRENCY)`, `structured()`가 진입 시점에 획득 |
| 재시도 | HTTP 실패 시 지수 백오프 + 지터, 최대 `MAX_HTTP_RETRIES`회 |
| JSON 복구 | `repair_json()` — 코드펜스 제거, 중괄호 보정, trailing comma 제거 후 재파싱 |
| 통계 | `_stats` — 호출 수/평균 latency/토큰/파싱실패/재시도 → `get_stats()`로 노출 |
| 모델 선택 | 생성자 `model=` 인자가 최우선, 없으면 `config.semantic_model`, 그것도 없으면 모듈 상수 `MODEL`(`LLM_MODEL` env) |

### 다중 모델(MODEL1/2/3) 지원 — 진입점별로 다르다

- **`run_robustness_test.py`**: `agent.config.get_models()`로 `LLM_MODEL1/2/3` 전부를 리스트로
  받아 **모델별로 전체 데이터셋 x 반복을 스윕**한다(비교 실험용).
- **`run_local.py`**(`asset-run-job.yaml`이 호출): `--model` 없으면 `get_models()[0]`
  (MODEL1 > MODEL2 > MODEL3 > MODEL 순 첫 값) **하나만** 골라 그 Job 전체(모든 csv)에 쓴다 —
  자산 하나당 한 번만 도는 구조라 모델 스윕 개념이 없다.
- **주의**: 이 둘은 대칭이 아니다. `asset-run-job.yaml`에 MODEL1/2/3을 여러 개
  넣어도 실제로는 첫 번째 것만 쓰인다 — 여러 모델로 같은 CSV를 비교하고 싶으면
  `robustness-job.yaml` 쪽을 써야 한다.

## 4. 설정 (`src/agent/config.py`) + `.env` 흐름

python-dotenv 없이 직접 파싱(`load_dotenv_file`). k8s에서는 이 `.env` 파일 대신
**Secret → envFrom → 컨테이너 환경변수**로 같은 값이 주입된다(아래 6절).

| 변수 | 기본값 | 용도 |
|---|---|---|
| `LLM_API_ENDPOINT` / `LLM_API_KEY` | - (필수) | OpenAI 호환 엔드포인트 |
| `LLM_MODEL` | `gpt-4o-mini` | 단일 모델 폴백 |
| `LLM_MODEL1/2/3` | (없음) | 다중 모델 (2절 참고) |
| `LLM_STRUCTURED_MODE` | `prompt` | 3절 표 참고 |
| `LLM_REQUESTS_PER_MINUTE` | 360 | RPM 스로틀 |
| `LLM_MAX_CONCURRENCY` | 8 (또는 12, `.env.example` 기준) | 동시 호출 상한 |
| `LLM_MAX_HTTP_RETRIES` | 3 | HTTP 재시도 횟수 |
| `AGENT_MAX_BATCH` | - | `MAX_BATCH`(planner.py, 코드 상수 8)와 별개로 k8s env로 전달되지만 실제 코드에서 읽는 곳은 없음(**죽은 설정값**, 아래 8절) |
| `ROBUSTNESS_REPS` | 3 | `run_robustness_test.py --reps` 기본값 |
| `K8S_NAMESPACE` | (없음) | `k8s/scripts/*.ps1`이 읽음 (7절) |

## 5. 로깅/관측성 (`src/agent/exp_logging.py` + 각 모듈)

`run_robustness_test.py`는 실행마다 `results/exp{N}_{KST 타임스탬프}/`를 새로 만들고
그 안에 `run.log`(stdout과 동시에 기록) + `robustness_results.jsonl`을 같이 둔다.
`run_local.py`/`asset-run-job.yaml`은 아직 exp 폴더 없이 `logging.basicConfig`로
stderr에만 찍는다(파일로 영구 저장 안 됨 — 8절 참고).

로그 이벤트 계층 (전부 `logger.info`, 레벨 WARNING이면 다 사라짐 — 8절의 `--quiet` 이슈 참고):

```
run_start        (runner.py)      자산 하나 처리 시작 - 행/열 수, 예산
  plan_done       (graph.py)        매 iteration: 뭘 골랐는지 / 왜 멈췄는지(stop_reason)
  batch_start     (executor.py)     이번 파도에 동시 실행되는 (skill,target) 목록
    skill_start   (executor.py)     스킬 1건 시작 (몇 번째 attempt인지)
      llm_call_queued   (llm.py)      세마포어 대기 진입
      llm_call_dispatch (llm.py)      실제 HTTP 요청 전송 시점
      llm_call / llm_call_retry / llm_call_failed  (llm.py)  결과 (latency/토큰 포함)
    skill_ok / skill_fail  (executor.py)  스킬 1건 종료
run_done          (runner.py)      자산 하나 종료 - elapsed/qps/tps
```

이 계층 덕분에 "어디서 멈췄는지"를 로그만 보고 추적할 수 있다 — 예:
`skill_start`는 찍혔는데 `llm_call_dispatch`가 없으면 세마포어 대기 중,
`llm_call_dispatch`는 찍혔는데 완료 로그가 없으면 네트워크/엔드포인트 문제.

## 6. k8s 배포 구조

```
                    ┌─────────────────────────┐
                    │ sh-ard-asset-agent-secret│  ← create-secret-from-env.ps1
                    │ (LLM_API_*, LLM_MODEL*,  │     (.env의 LLM_* 화이트리스트만)
                    │  LLM_STRUCTURED_MODE...) │
                    └────────────┬─────────────┘
                                 envFrom
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
      ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
      │ pod.yaml       │  │ robustness-job │  │ asset-run-job │
      │ (디버그, 상시)  │  │ .yaml (Job)    │  │ .yaml (Job)   │
      │ sleep infinity │  │ 여러 모델 x     │  │ csv마다        │
      │ kubectl exec용 │  │ 여러 csv x N회  │  │ run_local.py   │
      └───────┬────────┘  └───────┬────────┘  └───────┬────────┘
              │                   │                   │
              └───────────────────┼───────────────────┘
                                  ▼
                    ┌─────────────────────────┐
                    │ data-pvc.yaml (PVC)      │
                    │ accessModes: ReadWriteOnce│
                    │ /data/robustness_test/    입력 csv+metadata (두 Job 공유)
                    │ /data/results/             출력 (exp{N}/ 또는 <asset>.json)
                    └─────────────────────────┘
```

운영 스크립트(`k8s/scripts/`, 로컬 kubectl → 클러스터):

- `_common.ps1` — `.env` 파싱 + 네임스페이스 결정(파라미터 > `$env:K8S_NAMESPACE` > `.env`) 공용 함수
- `create-secret-from-env.ps1` — `.env`의 `LLM_*` 화이트리스트만 Secret으로 생성/갱신(`.env`의 다른 값, 예: `GH_PAT_TOKEN`이 새어나가지 않게)
- `upload-assets.ps1` — 로컬 csv/metadata를 PVC로. 디버그 파드가 없으면 띄운 뒤 `kubectl cp`
- `download-results.ps1` — PVC의 `/data/results`(또는 `-Target`으로 지정한 하위 경로)를 로컬로

## 7. CI/CD (`.github/workflows/build-push.yml`)

```
push (main 또는 exp/**)
  → test job: pytest -q (offline stub, LLM 키 불필요)
  → build-push job:
      1) docker build (deploy/Dockerfile) → ghcr.io/seongwonm/sh-ard-asset-agent
         태그: 브랜치명, 전체 커밋 SHA, (main이면) latest
      2) k8s/pod.yaml, robustness-job.yaml, asset-run-job.yaml의
         image 태그를 방금 빌드한 SHA로 sed 치환
      3) 변경 있으면 "ci: update image tag to <sha>" 커밋 → 같은 브랜치에 push
         (자기 SHA로 갱신되면 다음 트리거 때 diff 없어서 자동으로 멈추는
          self-limiting 패턴 — main/exp 어느 브랜치든 동일하게 동작)
```

이 덕분에 어느 브랜치에서 작업하든 `kubectl apply -f k8s/*.yaml`을 그대로 쓰면
**그 브랜치의 최신 이미지**가 돈다. (예전엔 main만 갱신했고 exp 브랜치는
`kubectl set image`로 수동 교체해야 했다 — 지금은 워크어라운드 불필요.)

## 8. 알려진 이슈 / 미해결

- **PVC가 `ReadWriteOnce`, 접근 파드가 여러 개**: `pod.yaml`(디버그, 상시)과
  `robustness-job.yaml`/`asset-run-job.yaml`(Job)이 같은 PVC를 마운트한다.
  디버그 파드가 PVC를 물고 있는 상태에서 Job이 스케줄되면(또는 반대 순서),
  스토리지 클래스/드라이버에 따라 마운트가 거부될 수 있다 — 아직 결정 안 됨
  (RWO→RWX 전환 여부, 아니면 디버그 파드/Job을 절대 동시에 안 띄우는 운영 규칙으로
  갈 것인지). **다음에 확정 필요.**
- **`AGENT_MAX_BATCH`가 k8s env로는 전달되지만 코드 어디서도 안 읽음**: planner.py의
  `MAX_BATCH=8`은 하드코딩 상수다. env로 오버라이드하려는 의도였다면 배선이
  안 됐거나, 애초에 env로 뺄 필요가 없었다면 매니페스트에서 지워야 한다.
- **`run_local.py`/`asset-run-job.yaml`은 exp 폴더가 없다**: `run_robustness_test.py`만
  `results/exp{N}_.../run.log`로 영구 로그를 남긴다. `asset-run-job.yaml`은
  stderr에만 찍고 파일로 안 남는다(Job pod가 24h 뒤 사라지면 로그도 같이 사라짐).
- **`git reflog`에만 남은 과거 구조**: `asset_agent/core` + `skills/structured_asset`
  라인(옛 `repair_ragged_csv` 등)이 브랜치 재구성 과정에서 어느 라이브 브랜치에서도
  도달 불가능해진 적이 있었다(`repair_ragged_csv`는 이번에 복구 완료, `csv_repair.py`).
  같은 방식으로 또 유실된 게 있는지는 확인 안 됨 — 필요하면 `git reflog` 전체를
  한 번 더 훑어야 한다.
- **`GH_PAT_TOKEN`이 로컬 `.env`에 평문으로 있음**: `.gitignore`돼 있어 커밋되진
  않지만, 로테이션 여부 확인 필요.
