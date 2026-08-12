# MIGRATION — SH-ARD-asset-agent 핵심 모듈 교체

기존 레포 `seongwonM/SH-ARD-asset-agent`의 **엔진만** 이 설계로 바꾼다.
바깥 껍데기(공개 API, 배치 스크립트, k8s, CI)는 그대로 둔다.

## 절대 바꾸지 않는 것

| 대상 | 이유 |
|---|---|
| `TableAssetContextBuilder.build()` 시그니처 | `run_from_csv.py`, `run_robustness_test.py`가 직접 호출 |
| 반환 JSON 7개 키 | `analyze_robustness_test.py`가 이 모양에 결합 |
| `examples/run_robustness_test.py` | **한 줄도 고치지 않는다** (아래 참조) |
| `examples/analyze_robustness_test.py` | 동일 |
| PVC 이름 `sh-ard-asset-agent-data` | 누적된 결과 JSONL과 이어달리기가 여기 있다 |
| Secret 이름/키 | `create-secret-from-env.ps1`이 참조 |

### run_robustness_test를 왜 반드시 살려야 하는가

이 스크립트는 데이터셋 12개 x column_descriptions 유무 2가지 x 모델 3종 x 20회
= 최대 1440회를 돌려 **반복시행 강건성과 모델별 차이**를 측정한다.
결과는 PVC에 누적되고 재실행 시 이어달리기를 한다.

출력 스키마를 바꾸면 **이미 쌓인 결과와 신규 결과를 비교할 수 없다.**
엔진을 바꾼 효과를 측정하려는 것인데 측정 기준선이 사라진다.
그래서 `src/agent/compat.py`가 옛 계약을 그대로 재현한다.

신규 정보(probe 검증, skill 선택 궤적)는 옛 키를 건드리지 않고
`asset_context.verification` / `trace`로 **추가만** 한다.
`analyze_robustness_test.py`는 모르는 키를 무시하므로 안전하다.

---

## 교체 대상

```
바꾼다
  asset_agent/skills/structured_asset/graph.py          → plan/act 루프
  asset_agent/skills/structured_asset/semantic_profiler.py → 컬럼 skill들로 분해
  asset_agent/skills/structured_asset/prompts.py        → SKILL.md 본문으로 이동

유지 + 재사용
  asset_agent/core/llm_client.py       RPM 스로틀. 새 deps가 감싼다
  asset_agent/core/json_utils.py       JSON 복구
  asset_agent/skills/.../csv_repair.py 깨진 CSV 복구
  asset_agent/skills/.../sampling.py   표본 추출
  asset_agent/core/config.py           HarnessConfig

신규
  src/agent/probes.py      데이터로 주장을 반증
  src/agent/planner.py     gap → 배치
  src/agent/compat.py      옛 계약 파사드
  skills/*/SKILL.md        판단을 선언으로
```

---

## Claude Code 프롬프트 (단계별)

한 번에 다 시키지 말 것. 각 단계 끝에 `make test`가 통과해야 다음으로 간다.

### 0단계 — 파악

```
이 레포는 기존 SH-ARD-asset-agent의 엔진을 교체하려는 프로젝트야.
먼저 README.md, CLAUDE.md, MIGRATION.md를 읽고,
src/agent/compat.py가 무엇을 보장하는지 설명해줘.
그 다음 make test로 31개 테스트가 통과하는지 확인해줘.

특히 이 질문에 답할 수 있어야 해:
run_robustness_test.py를 고치지 않고도 새 엔진을 쓸 수 있는 이유가 뭐야?
```

### 1단계 — 기존 레포에 이식

```
기존 레포 SH-ARD-asset-agent를 <경로>에 클론해뒀어.
이 프로젝트의 src/agent/ 와 skills/ 를 거기로 옮기되, 다음을 지켜:

1. asset_agent/skills/structured_asset/__init__.py의 export는 그대로 유지해.
   TableAssetContextBuilder, StructuredAssetConfig, repair_ragged_csv 세 개를
   계속 export해야 examples/*.py가 import 에러 없이 돈다.

2. TableAssetContextBuilder는 src/agent/compat.py의 것으로 교체해.
   단 생성자가 (client, config) 위치 인자를 계속 받아야 해 —
   run_robustness_test.py가 TableAssetContextBuilder(client=..., config=...)로 부른다.

3. StructuredAssetConfig는 없애지 말고 그대로 둬.
   robustness 스크립트가 semantic_model/description_model/search_model/
   requests_per_minute를 지정해서 모델별 비교를 한다. 이 필드들이 새 엔진의
   설정으로 흘러가도록 매핑해줘.

4. asset_agent/core/llm_client.py의 LLMClient는 버리지 말고
   RuntimeDeps가 내부에서 쓰도록 감싸. RPM 스로틀 상태를 배치 전체가
   공유해야 Gateway 한도를 지킨다.

옮긴 뒤 examples/run_from_csv.py가 import 에러 없이 뜨는지 확인해줘.
```

### 2단계 — 계약 회귀 테스트

```
tests/test_legacy_contract.py를 기존 레포로 함께 옮기고,
실제 analyze_robustness_test.py가 읽는 필드를 전부 커버하는지 확인해줘.

analyze_robustness_test.py를 읽고, 거기서 result[...]로 접근하는 키를
전부 뽑아서 테스트에 assert로 추가해. 하나라도 빠지면
1440회를 다 돌린 뒤에야 집계가 깨진 걸 알게 된다.
```

### 3단계 — 드라이런 비교

```
robustness 데이터셋 1개, reps=2, 모델 1개로 옛 엔진과 새 엔진을 각각 돌려서
결과 JSON을 비교해줘. 확인할 것:

- 7개 최상위 키가 동일한가
- asset_context_details의 6개 필드가 모두 있는가
- issues 항목이 {stage, error_type, message} 3키인가
- performance에 옛 8개 키가 다 있는가

다른 점이 있으면 compat.py를 고쳐. 스크립트를 고치지 마.
```

### 4단계 — k8s 반영

```
k8s/ 아래 매니페스트를 기존 레포 것과 병합해줘.

- data-pvc.yaml: 이름은 그대로, storage만 1Gi→4Gi
  (trace/verification이 추가돼 JSONL 한 줄이 커진다)
- pod.yaml: envFrom secretRef로 단순화하고
  LLM_STRUCTURED_MODE / AGENT_MAX_BATCH / PYTHONPATH 추가, 메모리 상한 상향
- robustness-job.yaml: 신규. exec 대신 Job으로 장시간 배치를 돌린다

k8s/scripts/*.ps1은 건드리지 마. run-robustness-test.ps1은 계속 동작해야 한다
(-FetchOnly 모드로 Job이 도는 중에 중간 결과를 받는 용도로도 쓴다).

deploy/Dockerfile에 skills/ 와 src/ 를 COPY하는 줄을 추가하고
PYTHONPATH에 /app/src를 넣어줘.
```

---

## k8s 세팅 절차

기존 절차를 그대로 따르되 두 군데가 늘었다.

```powershell
# 1. Secret (기존과 동일 — .env에서 읽는다)
./k8s/scripts/create-secret-from-env.ps1

# 2. PVC (최초 1회)
kubectl apply -f k8s/data-pvc.yaml

# 3-A. 대화형 디버그 Pod
kubectl apply -f k8s/pod.yaml
kubectl exec -it sh-ard-asset-agent -- /bin/bash
  $ make test                                    # mock으로 전 구간 검증
  $ python examples/run_from_csv.py /data/x.csv my_asset

# 3-B. 장시간 강건성 배치 (신규 — Job)
kubectl apply -f k8s/robustness-job.yaml
kubectl logs -f job/sh-ard-robustness
```

`.env`에 추가할 값:

```
LLM_STRUCTURED_MODE=guided_json   # 또는 json_schema. vLLM 버전 확인 필수
AGENT_MAX_BATCH=8
AGENT_MAX_LLM_CALLS=300
```

### structured output 모드는 반드시 직접 확인할 것

`guided_json`(구형)과 `json_schema`(신형)는 vLLM 버전에 따라 다르다.
**틀려도 에러가 나지 않는다** — 옵션이 조용히 무시되고 스키마 강제 없이 돈다.
확인 방법: 스키마에 없는 필드를 유도하는 프롬프트를 보내 실제로 차단되는지 본다.
차단되지 않으면 모드가 틀린 것이다.

### 배치 크기와 RPM

Gateway RPM=360이면 초당 6건이다. 응답 지연 2초 가정 시 동시 12건까지 채울 수
있고(Little's Law), `AGENT_MAX_BATCH`는 그 안에서 움직여야 한다.
기존 `semantic_max_workers=12`와 같은 계산이다 — 실측 지연이 달라지면
둘 다 같이 조정한다.

---

## 마이그레이션 후 첫 측정

옛 엔진과 새 엔진의 차이를 보려면 **같은 데이터셋·같은 모델**로 돌려야 한다.

```powershell
# 옛 결과 백업 (덮어쓰지 말 것)
kubectl cp sh-ard-asset-agent:/data/robustness_results.jsonl ./results/before.jsonl
kubectl exec sh-ard-asset-agent -- mv /data/robustness_results.jsonl /data/robustness_before.jsonl

# 새 엔진으로 재실행
kubectl apply -f k8s/robustness-job.yaml
```

새로 볼 수 있게 된 지표:

| 지표 | 위치 | 의미 |
|---|---|---|
| `probe_coverage` | `asset_context.verification` | 주장 중 데이터로 검증 가능했던 비율 |
| `refuted` | 동일 | 반증되어 폐기된 주장 |
| `unverified_count` | 동일 | 검증 수단이 없는 주장 = 사람이 볼 큐 |
| `blocked` | `performance` | 반복 실패로 격리된 (skill, 컬럼) |
| `trace` | 최상위 | 회차별 skill 선택 궤적 |

**반복시행 강건성을 볼 때 `trace`를 함께 보라.** 같은 입력인데 결과가
달랐다면, 그게 LLM 출력 차이인지 skill 선택 경로 차이인지 여기서 갈린다.
옛 엔진은 경로가 고정이라 이 구분 자체가 불가능했다.
