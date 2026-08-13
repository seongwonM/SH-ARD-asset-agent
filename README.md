# asset-context-agent

테이블을 받아 컬럼을 해석하고 asset context를 만드는 에이전트.
**어떤 skill을 쓸지 매 스텝 다시 결정하고, 만들어낸 주장을 데이터로 반증한다.**

```
START → plan ──(목표 충족/예산 소진/후보 없음)──→ END
          │
      (독립 작업 배치)
          ↓
        act ────────────────────────────────→ plan
```

노드는 둘뿐이다. 작업 종류는 `skills/` 폴더가 정한다.

## 실행 예시

컬럼 5개짜리 테이블. **이 순서는 어디에도 적혀 있지 않다.**

```
[plan] gap 8개 중 'profile-table' 선택
  [OK] profile-table                            slots=[table_profile]
[plan] gap 8개 → 독립 작업 5건 병렬 실행
  [OK] categorical-code::verdict      claims=2  tries=2
  [OK] identifier-link::equipment_id  claims=1  tries=2
  [OK] identifier-link::run_id        claims=1  tries=1
  [OK] numeric-measure::power_value   claims=0  tries=2
  [OK] temporal-axis::run_at          claims=1  tries=2
[plan] gap 6개 중 'grain-resolution' 선택
  [OK] grain-resolution               claims=1  tries=2
[plan] gap 5개 중 'verify-context' 선택
  [OK] verify-context
[plan] gap 4개 중 'synthesize-context' 선택
  [OK] synthesize-context             slots=[topic, summary, search_terms, asset_context]

종료: goal_reached | iterations=5 | LLM=14 probe=11
```

`tries=2`인 4건은 전부 **데이터가 주장을 반증해서** 재시도된 것이다.

## 핵심 아이디어 3가지

### 1. 단계가 아니라 슬롯

파이프라인 단계 대신 "채워야 할 빈칸"으로 모델링한다.

```
table_profile → column_semantics → grain / linkage → verification → topic / summary → asset_context
```

단계는 순서를 강제하지만 슬롯은 "무엇이 비었는가"만 말한다.
그래서 플래너가 순서를 매번 새로 정할 수 있다.

`column_semantics`는 컬럼별로 채워지므로 gap이 **"어느 컬럼이 남았다"**로 나온다.
이게 per-column skill 배정과 병렬 배치의 근거다.

### 2. probe — 데이터로 주장을 반증한다

structured output은 **형식만** 보장한다. `{"unit": "kPa"}`는 스키마상 유효하다.
텍스트 가드는 "입력에 kPa가 없다"까지는 잡지만,
**"이 컬럼은 primary key다"는 잡지 못한다.** 그 진위는 텍스트가 아니라 데이터에 있다.

그래서 skill이 검사 가능한 주장을 하면 그것을 **깨뜨릴 probe**를 함께 낸다.

```python
result.claims.append(VerifiableClaim(
    statement=f"{ctx.target}는 {out.role} 역할의 식별자다",
    probe=ProbeRequest(kind=ProbeKind.UNIQUENESS, columns=[ctx.target],
                       params={"min_ratio": ROLE_MIN_UNIQUENESS[out.role]}),
    critical=out.role == "primary",
))
```

executor가 실행하고, 실패하면 **실측값이 그대로 재시도 힌트가 된다.**

```
'equipment_id는 primary 역할의 식별자다' 주장이 데이터와 맞지 않는다.
['equipment_id']의 실제 유일성 비율은 0.5000이다(기준 0.99). 중복 예시: [{'equipment_id': 'C03'}, ...]
```

LLM이 만든 힌트보다 데이터에서 나온 힌트가 교정에 훨씬 강하다.

probe는 **반증** 도구다. 통과가 참을 증명하지 않고, 실패가 거짓을 증명한다.
그래서 `verify-context`는 "전부 맞다"고 말하지 않는다.

```
검증: 통과 4 / 반증 0 / 검증불가 5 | probe 검증률 0.444
  [OK ] run_id의 역할은 primary이다
  [OK ] ['run_id'] 조합이 행을 유일하게 식별한다
  [ ? ] run_id: 개별 공정 실행을 식별하는 값이다 (자연어 주장이라 데이터 검증 불가)
```

**`unverified` 목록이 사람이 봐야 할 큐다.** 검증률 0.44를 숨기지 않는 것이 요점이다.

### 3. 배치 실행

컬럼 200개에서 한 번에 하나씩 고르면 200번의 plan-act 왕복이 생기고,
그 중 199번은 "또 다음 컬럼"이라는 자명한 판단이다.

keyed 슬롯에 서로 다른 key로 쓰는 작업은 충돌하지 않으므로 한 파도로 묶는다.
단일 값 슬롯(`grain`, `summary`)에 쓰는 작업은 항상 단독 실행한다 —
두 skill이 같은 슬롯에 동시에 쓰면 마지막 승자가 비결정적이다.

실측 (`batch_limit=8`):

| 컬럼 수 | 컬럼 해석 파도 | 전체 iteration | 순차였다면 |
|---|---|---|---|
| 5 | 1 | 5 | 5파도 |
| 20 | 3 | 7 | 20파도 |
| 60 | 8 | 12 | 60파도 |

## Evidence-first 전환

2026-08-12 기준 현재 구조는 `hypothesis-first`에서 `evidence-first`로 옮겨가는 중이다.

- observer skill이 rule-based evidence를 먼저 만든다
- evidence artifact를 board에 누적한다
- `analysis-planning` skill이 evidence를 읽고 다음 분석 우선순위를 정한다
- interpreter skill은 raw data보다 evidence와 profile을 근거로 해석한다
- verifier와 synthesizer는 기존처럼 뒤를 받친다

자세한 설계와 유지보수 원칙은 `ARCHITECTURE.md` 참고.

## skill 선택 로직

```
1. gap에 기여하는가        provides ∩ open_gaps
2. 선행 슬롯이 완결됐는가   requires 가 gap에 없어야 함 (부분 충족 아님)
3. 이 데이터에 적용되는가   applies_when
4. 점수                    gap 기여도 + kind 특화 보너스 - 비용
```

전부 결정론적이다. **"왜 이 컬럼에 이 skill이 갔나"에 답할 수 있어야
임계값 튜닝이 가능하기 때문**이다. 점수 동률일 때만 LLM tie-break를 붙일 수 있다.

`requires` 판정은 3단으로 완화된다.

| pass | 조건 | 필요한 이유 |
|---|---|---|
| 1 strict | 선행 슬롯에 **남은 gap이 없어야** 함 | `board.has()`만 보면 컬럼 1개만 해석돼도 True가 되어, 5개 중 2개만 끝난 상태에서 최종 합성이 실행된다 |
| 2 partial | 선행 슬롯이 **하나라도 채워졌으면** 통과 | 컬럼 하나가 blocked되면 그 슬롯은 영원히 완결되지 않는다 |
| 3 unreachable | **채울 skill이 전부 blocked인 슬롯은 면제** | 선행 skill이 죽으면 후속 전체가 영구 정지한다 |

3번은 넓은 테이블 테스트를 non-vacuous하게 고치는 과정에서 발견된 구멍이다.
`grain-resolution`이 blocked되자 `synthesize-context`가 영원히 실행되지 못하고
산출물 없이 종료됐다. 완화 조건이 `board.has()`라 **아예 비어 있는 슬롯**은
2번 pass로도 구제되지 않았기 때문이다.

## 근거

| 요소 | 출처 |
|---|---|
| SKILL.md + frontmatter, progressive disclosure | Anthropic Agent Skills |
| 공유 상태 + 매 스텝 기여자 선택 | Blackboard architecture (Hayes-Roth, 1985) |
| plan–act 루프 | ReAct / LangGraph plan-and-execute |
| 반증 가능성 기반 검증 | Popper. 통과가 아니라 실패가 정보를 준다 |
| structured output + 스키마 강제 | vLLM guided decoding, Pydantic |

## 구조

```
CLAUDE.md              작업 지침 (불변식, 확장 방법, 금지사항)
HANDOFF.md             남은 작업 백로그 (P0/P1/P2 + 알려진 한계)
.claude/commands/      /new-skill, /trace, /add-probe
src/agent/
  contract.py          Slot, SkillManifest, VerifiableClaim — 유일한 확장 지점
  probes.py            데이터 기반 반증 (9종)
  state.py             Blackboard. board가 단일 저장소
  registry.py          skills/ 스캔, frontmatter 파싱, 본문 lazy load
  planner.py           gap 계산 → 점수화 → 배치 구성
  executor.py          병렬 실행 → 3단 검증 → 재시도 → 실패 격리
  guards.py            텍스트 근거 대조
  llm.py               vLLM structured output
  graph.py             plan/act 2노드 루프
skills/                9개 (+_template)
tests/                 22 passed
```

## 시작하기

```bash
make install
make test            # vLLM 없이 mock으로 전 구간 실행
make trace           # 궤적 출력
```

## 성능 측정

vLLM 서버만 있으면 된다(k8s 불필요). 자세한 건 `BENCH.md`.

코드 가설을 병렬로 검증할 때는 `git worktree` 기반의 `EXPERIMENTS.md` 규칙을 따른다.

```bash
make check           # 엔드포인트 점검 — structured output이 실제로 강제되는지
make mock            # 정답지 포함 mock 데이터 생성
make bench-offline   # LLM 없이 하네스 오버헤드 (비용 0)
make bench           # 실제 측정
```

측정을 두 종류로 나눈다. **정답 대비 정확도**(kind/role/grain/해상도/PII/함수종속)와
**정답 없이 재는 것**(비용/재시도율/커버리지/검증률/반복 일관성)이다.
섞어서 보면 숫자가 거짓말을 한다 — 반복 일관성 1.00은 같은 오답을 20번 낸 경우에도 나온다.

실제 연결은 `src/agent/llm.py: RuntimeDeps._post()` 하나만 구현하면 된다.
나머지는 `HANDOFF.md` 참고.

## K8s에서 내부 데이터 실행

내부 CSV 여러 건을 로컬과 같은 경로로 돌리려면 [`asset-run-job.yaml`](k8s/asset-run-job.yaml) 사용.

전제:
- `/data` PVC에 `<asset>.csv` + `<asset>_metadata.json`(선택) 업로드
- `sh-ard-asset-agent-secret`에 `LLM_API_ENDPOINT`, `LLM_API_KEY`, `LLM_MODEL` 존재

순서 (PowerShell):

```powershell
# 0. Secret — .env의 LLM_* 값으로 생성/갱신 (최초 1회 + .env 바뀔 때마다)
./k8s/scripts/create-secret-from-env.ps1

# 1. PVC (최초 1회)
kubectl apply -f k8s/data-pvc.yaml

# 2. 로컬 데이터를 PVC로 업로드 (data/internal/*.csv + *_metadata.json)
#    -Target 기본값은 robustness_test이므로 asset-run-job용은 internal을 명시한다.
./k8s/scripts/upload-assets.ps1 -LocalDir .\data\internal -Target internal

# 3. 실행
kubectl apply -f k8s/asset-run-job.yaml
kubectl logs -f job/sh-ard-asset-run

# 4. 결과를 로컬로 회수
./k8s/scripts/download-results.ps1 -LocalDir .\results
```

결과 JSON은 기본값으로 `/data/results/<asset>.json`에 자산별로 저장된다.

같은 순서로 `k8s/robustness-job.yaml`을 돌릴 땐 2번의 `-Target internal`을 빼면 된다
(기본값이 `robustness_test`): `./k8s/scripts/upload-assets.ps1 -LocalDir .\data\robustness_test`.
4번은 `-Target exp3_202608131000`처럼 실험 폴더를 지정해 그 실험 하나만
(run.log + robustness_results.jsonl) 받을 수 있다.
