# asset-context-agent

CSV 테이블을 받아 **컬럼이 무엇을 뜻하는지** 해석하고, 그 해석을 **데이터로 반증**한다.

```bash
make install
make test                      # vLLM 없이 전 구간 (가짜 LLM, 41 tests)
cp .env.example .env           # LLM_API_ENDPOINT / LLM_MODEL 채우기
make check                     # 엔드포인트 점검
python run.py ./data.csv --output result.json
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
run.py                     진입점(shim). k8s Job이 이 경로를 잡고 있다
src/column_semantics/
  core/                    DataFrame -> 사실. 순수 계산, LLM/파일/환경변수를 모른다
    profiling.py             컬럼별 프로파일
    relations.py             컬럼 쌍 관계 / 복합키 후보 / 관계 그룹
    evidence.py              위 둘을 묶은 증거 블록
    probes.py                skill이 요청한 검사식 평가 (안전한 산술/비교 계산기)
    timeline.py, clock.py    실행 궤적
  adapters/                바깥 세계. 여기만 갈아끼우면 다른 환경에 붙는다
    csv_source.py            인코딩 추정 + 깨진 행 복구
    llm.py                   OpenAI 호환 엔드포인트 (LLMClient 프로토콜)
    skills.py                skills/*.md 로딩
    env.py, ratelimit.py
  pipeline/                조립. 순서와 병렬화
    plan.py                  고정 순서 + LLM 계획 출력 정제
    skill_runner.py          skill별 payload 조립
    orchestrator.py          실행 루프
  app.py                   composition root (CLI도 실험도 여기를 부른다)
  cli.py
skills/*.md                skill = 프롬프트 파일 하나. 등록 절차 없음
experiments/               실험 전용. 제품 경로가 아니다
k8s/                       배치 Job / 디버그 Pod / PVC
```

의존 방향은 `pipeline -> (core, adapters)` 한 방향이다. core가 adapters를
import하면 "LLM 없이 프로파일링만 검증"이 불가능해진다.

## 실행 흐름

```
1차   semantic_type → column_interpretation(컬럼별 병렬) → relation_analysis*
gap   gap_planner가 컬럼별로 부족한 점 판단 → 배정된 보충 skill 병렬 실행
검증  관계 그룹별 semantic_validation 병렬 → probe로 실측 대조
마무리 table_context
수정  needs_revision이면 planner가 재계획 → 해당 skill만 재실행 → 재검증
```

`*` pairwise 증거가 하나도 없으면 relation_analysis는 호출하지 않는다.

**1차 순서는 고정이라 LLM에게 묻지 않는다.** 판단할 여지가 없는 곳에 계획
호출을 넣으면 비용만 늘고 재현성이 떨어진다. LLM이 계획하는 지점은 두 곳뿐이다
— 컬럼별 보충(`gap_planner`)과 검증 실패 후 재계획(`planner`).

## 결과 JSON

```json
{
  "meta":     { "status", "validation_status", "llm_model", "started_at", ... },
  "plans":    [ 재계획 라운드별 스텝 ],
  "evidence": { "table", "column_profiles", "relation_evidence", "grain_candidates" },
  "results":  { "semantic_type", "column_interpretation", "relation_analysis",
                "semantic_validation", "table_context" },
  "timeline": [ 각 skill/LLM 호출/probe의 시작·종료 (KST) ]
}
```

skill이 끝날 때마다 `<output>.partial.json`에 체크포인트를 남긴다. 끝까지
성공하면 최종 파일로 대체되고 partial은 지워진다 — 중간에 죽어도 그때까지의
skill 출력은 남는다.

`meta.validation_status`가 `unresolved_after_max_rounds`면 반증된 주장을
끝내 해소하지 못한 채 끝난 것이다. `done`만 보고 넘어가면 안 된다.

## skill 추가

`skills/<이름>.md` 파일 하나를 만들고, `pipeline/plan.py`에 언제 도는지 적는다.
등록 절차는 없다 — 폴더를 읽는다. 자세한 절차는 `.claude/commands/new-skill.md`.

## 배치 / k8s

```bash
# 로컬: 폴더 안 CSV 전부
make batch DATA=./data OUT=./results

# k8s: 이미지 안 코드로 바로 돈다 (CI가 커밋 SHA로 이미지 태그를 갱신)
kubectl apply -f k8s/data-pvc.yaml
kubectl apply -f k8s/column-poc-job.yaml
kubectl logs -f job/column-poc-batch
./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results
```

이미지를 다시 굽지 않고 코드만 바꿔 돌려보려면
`./k8s/scripts/upload-column-poc.ps1`로 PVC에 올린다. Job은 PVC에 코드가 있으면
그쪽을 쓴다 — **실험이 끝나면 반드시 지울 것.** 안 지우면 낡은 코드가 이후 모든
Job에서 조용히 계속 쓰인다.
