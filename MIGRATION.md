# MIGRATION

`remove-compat` 이후 기준 문서.

현재 활성 구조는 레거시 호환 파사드 없이 직접 runner를 쓴다.

- 실행 진입점: `src/agent/runner.py`
- 로컬 실행: `examples/run_local.py`
- 벤치/강건성 실행: `bench/run_bench.py`, `examples/run_robustness_test.py`
- 핵심 결과 블록: `column_analysis`, `data_interpretation`, `asset_context`

## 유지하는 것

| 대상 | 이유 |
|---|---|
| 입력 데이터의 원본 컬럼과 의미 | 실제 실험/비교에서 보존해야 하는 축 |
| dataset x column_descriptions 유무 x model x rep | robustness 비교 축 |
| `asset_context` 중심 결과 | 최종 사용자 산출물 |

## 자유롭게 바꿀 수 있는 것

| 대상 | 원칙 |
|---|---|
| mock 데이터 값 분포/텍스트 다양성 | 원본 컬럼 의미만 유지하면 자유롭게 고도화 |
| 중간 산출물 구조 | `column_analysis`, `data_interpretation`, `asset_context`만 유지 |
| robustness 결과 JSONL의 부가 필드 | 비교/분석에 유리하면 추가 가능 |

## 현재 구조 요약

```
src/agent/graph.py        plan/act 루프
src/agent/planner.py      gap 기반 배치 선택
src/agent/executor.py     skill 실행 + probe/guard 검증
src/agent/runner.py       현재 표준 실행 진입점
src/agent/llm.py          OpenAI-compatible runtime deps
skills/*                  선언형 skill + handler
examples/run_local.py     단일 CSV 실행
examples/run_robustness_test.py  반복 강건성 실행
bench/run_bench.py        mock truth 기준 벤치
```

---

## 현재 변경 원칙

1. `compat.py`는 더 이상 유지하지 않는다.
2. 활성 코드 경로는 모두 `TableAssetContextRunner`를 사용한다.
3. 문서/테스트/벤치는 현재 runner 결과 구조를 기준으로 유지한다.
4. mock 데이터는 단순 예제가 아니라, 실제 분포/반례/자유 텍스트를 포함하는 방향으로 키운다.

## k8s 반영

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

`k8s/scripts/create-secret-from-env.ps1`과 `run-robustness-test.ps1`은 "건드리지 마"
라는 지시와 달리 실제로는 이 레포에 마이그레이션되지 않았었다(기존 레포에만 존재).
`create-secret-from-env.ps1`은 이후 이 레포 기준으로 새로 작성해 채워 넣었고,
`run-robustness-test.ps1`(exec 기반 배치 + `-FetchOnly`)은 그 역할을 `robustness-job.yaml`
(Job)이 대신하므로 복원하지 않았다. 대신 로컬 데이터를 PVC에 넣는 `upload-assets.ps1`을
새로 추가했다(기존 레포에는 없던 스크립트 — 그동안 수동 `kubectl cp`로 하던 것을 대체).

```powershell
# 1. Secret — .env의 LLM_* 값으로 생성/갱신
./k8s/scripts/create-secret-from-env.ps1

# 2. PVC (최초 1회)
kubectl apply -f k8s/data-pvc.yaml

# 3. 로컬 데이터를 PVC로 업로드
./k8s/scripts/upload-assets.ps1 -LocalDir .\data\internal
./k8s/scripts/upload-assets.ps1 -LocalDir .\data\robustness_test -Target robustness_test

# 4-A. 대화형 디버그 Pod
kubectl apply -f k8s/pod.yaml
kubectl exec -it sh-ard-asset-agent -- /bin/bash
  $ make test                                    # mock으로 전 구간 검증
  $ python examples/run_local.py /data/internal/x.csv my_asset

# 4-B. 장시간 강건성 배치 (Job)
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

## 다음 작업

- mock 데이터셋을 더 현실적으로 확장
- `examples/run_robustness_test.py` 결과 집계 스크립트 추가/정리
- 실제 실험 데이터셋으로 kind/grain/probe 임계값 보정
