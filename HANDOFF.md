# HANDOFF

Claude Code에서 이어서 작업할 때 읽는 문서.
**작동하는 것 / 아직 아닌 것 / 다음에 할 것**을 구분해서 적었다.

## 지금 상태

```bash
make install
make test     # 22 passed
make trace    # 실행 궤적
```

vLLM 없이 mock으로 전 구간이 돈다. LLM 연결부만 비어 있다.

실측 궤적 (컬럼 5개):

```
[plan] gap 8개 중 'profile-table' 선택
  [OK] profile-table                            slots=[table_profile]
[plan] gap 8개 → 독립 작업 5건 병렬 실행
  [OK] categorical-code::verdict      claims=2  tries=2   ← 없는 카테고리 반증
  [OK] identifier-link::equipment_id  claims=1  tries=2   ← 거짓 PK 반증
  [OK] identifier-link::run_id        claims=1  tries=1
  [OK] numeric-measure::power_value   claims=0  tries=2   ← 근거 없는 단위 차단
  [OK] temporal-axis::run_at          claims=1  tries=2   ← 과도한 해상도 반증
[plan] gap 6개 중 'grain-resolution' 선택
  [OK] grain-resolution               claims=1  tries=2   ← 유일하지 않은 키 반증
[plan] gap 5개 중 'verify-context' 선택
  [OK] verify-context                 (LLM 미사용)
[plan] gap 4개 중 'synthesize-context' 선택
  [OK] synthesize-context             slots=[topic, summary, search_terms, asset_context]

종료: goal_reached | iterations=5 | LLM=14 probe=11
검증: 통과 4 / 반증 0 / 검증불가 5 | probe 검증률 0.444
```

---

## P0 — 실제로 돌리려면 반드시 필요

### 1. vLLM 연결 (`src/agent/llm.py: RuntimeDeps._post()`)
현재 `NotImplementedError`. 주석에 구현 예시가 있다.
```python
from openai import AsyncOpenAI
client = AsyncOpenAI(base_url=BASE_URL, api_key="EMPTY")
extra = payload.pop("extra_body", None)
r = await client.chat.completions.create(**payload, extra_body=extra)
return r.choices[0].message.content
```

### 2. structured output 방식 확정
`VLLM_STRUCTURED_MODE` 환경변수로 `guided_json`(구형) / `json_schema`(신형)를 고른다.
**서빙 중인 vLLM 버전 문서를 직접 확인할 것** — 표기가 버전마다 바뀌었고
이 코드의 기본값이 맞다는 보장이 없다.
확인 방법: 스키마 위반을 유도하는 프롬프트를 보내 실제로 차단되는지 본다.
차단되지 않으면 옵션이 무시되고 있는 것이다.

### 3. 데이터 소스 연결 (`RuntimeDeps.dataframe()`)
현재 메모리 dict만 지원. `db://`, `s3://` 스킴별 로더 필요.
**probe가 데이터를 반복 조회하므로 여기가 성능 병목이 된다.**
큰 테이블은 probe용 샘플을 미리 만들어 캐싱하는 편이 낫다
(다만 uniqueness는 샘플로 판정하면 안 된다 — 전수 또는 SQL `COUNT(DISTINCT)`로).

### 4. kind 판정 임계값 검증
`skills/profile_table/handler.py`의 규칙은 4행짜리 표본으로만 검증했다.
실제 테이블로 오분류를 확인할 것. 이미 두 번 틀렸다.
- `pd.to_datetime(501)`이 성공해 정수가 temporal로 분류됨 → 수치 dtype 제외로 수정
- 행이 적을 때 `distinct_ratio`만 보면 categorical을 놓침 → 절대 개수 병행으로 수정

---

## P1 — 품질에 직접 영향

### 5. probe 검증률 개선 (현재 0.44)
`verify-context` 리포트의 `unverified` 대부분이 자연어 `meaning`이다.
검증 수단이 없는 주장이 절반이라는 뜻.

개선 방향 두 가지:
- **주장을 쪼갠다.** "출력값을 W 단위로 기록한다" → 단위 주장은 텍스트 근거로,
  "출력값" 부분은 numeric range probe로 분리 검증
- **LLM-as-judge를 추가한다.** 자연어 의미의 타당성은 probe로 못 잡는다.
  단 judge 자체가 검증되지 않으므로 `unverified`가 `judged`로 바뀔 뿐임을 명심할 것.
  judge를 넣는다면 골든 라벨로 judge의 정확도를 먼저 측정해야 한다.

### 6. 평가 세트 구축
지금은 mock이 정답을 알고 있는 상황만 테스트한다.
실제 테이블 10~20개에 사람이 만든 기대 출력을 붙여야 회귀를 잡을 수 있다.
측정할 지표:
- 컬럼 kind 분류 정확도 (결정론적이라 가장 먼저 고정 가능)
- probe 반증률 (높으면 프롬프트 문제, 0이면 probe가 느슨한 것)
- 컬럼 커버리지 (`blocked` 비율)
- 사람 평가: 요약의 사실성

### 7. LLM tie-break 연결
`planner.build_tiebreak_messages()`는 작성돼 있으나 그래프에 연결되지 않았다.
`Decision.needs_tiebreak`가 True일 때만 호출하면 된다.
**주의**: 점수 동률일 때만 개입시킬 것. 주 선택자로 만들면 재현성이 사라진다.

### 8. 체크포인트/재개
`build_agent(checkpointer=...)`는 받아만 두고 쓰지 않는다.
LangGraph `AsyncSqliteSaver`를 붙이면 중단 지점에서 재개된다.
컬럼 200개 테이블에서 중간 실패 시 전부 다시 하는 것을 막는다.

---

## P2 — 확장

### 9. 자산 간 관계 (Group Context)
현재는 단일 테이블만 본다. `linkage` 슬롯에 컬럼별 role과 유일성이 이미 쌓이므로,
여러 자산의 linkage를 모아 조인 후보를 만드는 것이 자연스러운 다음 단계다.

**probe가 여기서 진가를 발휘한다.** 조인 가능성 주장은
값 overlap probe로 실제 반증할 수 있다 (`ProbeKind`에 추가 필요).
임베딩 유사도만으로 조인 후보를 내는 방식보다 훨씬 강하다.

### 10. 도메인 skill
현재 9개는 전부 범용이다. 반도체 공정이면:
- 레시피/스펙 파라미터 컬럼 (상하한이 별도 테이블에 있는 경우)
- lot/wafer 계층 식별자 (계층 관계를 `FUNCTIONAL_DEP` probe로 검증 가능)
- 알람/이벤트 코드 체계

`/new-skill` 커맨드로 스캐폴딩한다.

### 11. 관찰성
`history`에 전부 남지만 사람이 읽기 어렵다.
run별 JSON을 `runs/<asset_id>/<timestamp>.json`으로 떨구고,
plan 결정과 probe 결과를 타임라인으로 렌더링하면 튜닝이 빨라진다.

---

## 알려진 한계

- **probe는 반증만 한다.** 통과했다고 맞는 게 아니다. `verified`를 "검증됨"으로
  읽으면 안 되고 "반증되지 않음"으로 읽어야 한다.
- **자연어 요약은 검증되지 않는다.** 현재 구조로는 원리적으로 불가능하다.
  `unverified` 목록이 그 사실을 숨기지 않고 드러내는 것이 최선이다.
- **부분 충족 완화가 조용히 품질을 떨어뜨릴 수 있다.** 컬럼 하나가 blocked되면
  플래너가 완화 pass를 열어 진행한다. `plan_note`의 `[부분 충족 상태로 진행]`과
  `asset_context.coverage`를 반드시 함께 볼 것.
- **배치 크기가 LLM 동시성과 연동되지 않았다.** `MAX_BATCH=8`과
  `VLLM_MAX_CONCURRENCY=8`이 우연히 같을 뿐, 코드로 묶여 있지 않다.
- **3차 완화(도달 불가 슬롯 면제)는 조용히 품질을 떨어뜨린다.** 선행 skill이
  전부 실패해도 후속이 진행되므로, `plan_note`의 `[도달 불가 슬롯 면제: ...]`를
  경보로 취급할 것. 이게 뜨면 해당 슬롯이 통째로 비어 있다는 뜻이다.
- **테스트가 vacuous하게 통과할 수 있다.** mock이 반환하는 값과 픽스처 데이터가
  어긋나면 모든 작업이 probe에 막혀 0건 처리되고, "파도 수가 적다"는 단언이
  0 <= N으로 통과한다. 실제로 겪었다. 확장성/성능 단언에는 **처리 건수 단언을
  반드시 함께** 둘 것 (`test_batching_scales_with_column_count` 참고).
