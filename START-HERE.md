# START-HERE

Claude Code에서 이 프로젝트를 처음 여는 사람을 위한 문서.

## 0. 동작 확인 (2분)

```bash
cd asset-context-agent
pip install -r requirements.txt -r requirements-dev.txt
make test     # 22 passed 나와야 함
make trace    # 실행 궤적 출력
```

vLLM 없이 mock으로 전 구간이 돕니다. 여기서 실패하면 환경 문제입니다.

## 1. Claude Code 실행

```bash
cd asset-context-agent
claude
```

`CLAUDE.md`가 자동으로 읽힙니다. `/init`은 실행하지 마세요 — 이미 작성된
`CLAUDE.md`를 덮어쓸 수 있습니다.

## 2. 첫 세션에서 붙여넣을 프롬프트

프로젝트 구조를 먼저 파악시키는 게 좋습니다. 아래를 그대로 붙여넣으세요.

```
README.md, CLAUDE.md, HANDOFF.md를 읽고 이 프로젝트를 파악해줘.
그 다음 make test로 동작을 확인하고, 아키텍처를 한 문단으로 요약해줘.
특히 planner가 skill을 고르는 3-pass 완화 로직과 probe 계층이
왜 필요한지 이해했는지 확인하고 싶어.
```

이해가 맞는지 확인한 뒤 실제 작업으로 넘어가세요.

## 3. 사용 가능한 슬래시 커맨드

| 커맨드 | 용도 |
|---|---|
| `/new-skill <이름> <용도>` | 새 skill 스캐폴딩 + 계약 검증 |
| `/add-probe <이름> <검증 대상>` | 새 probe 종류 추가 |
| `/trace [관심사]` | 실행 궤적 분석 |

`.claude/commands/`에 정의돼 있습니다. 커맨드 내용을 고치면 즉시 반영됩니다.

## 4. 작업 순서 제안

`HANDOFF.md`에 P0/P1/P2로 정리돼 있습니다. 처음 시작한다면:

**1) vLLM 연결부터** (`src/agent/llm.py: RuntimeDeps._post()`)

```
src/agent/llm.py의 RuntimeDeps._post()를 openai 라이브러리로 구현해줘.
주석에 예시가 있어. 구현 후 실제 엔드포인트 없이도 import가 깨지지 않는지
make test로 확인해줘.
```

**2) structured output 방식 검증**

이건 Claude에게 시키기보다 직접 확인하는 게 낫습니다.
서빙 중인 vLLM 버전 문서를 보고 `guided_json` / `json_schema` 중 어느 쪽인지 확정하세요.
확인 방법: 스키마 위반을 유도하는 프롬프트를 보내 실제로 차단되는지 봅니다.
차단되지 않으면 옵션이 무시되고 있는 겁니다.

**3) 실제 테이블로 kind 판정 검증**

```
skills/profile_table/handler.py의 _classify() 규칙을 실제 테이블로 검증하고 싶어.
<테이블 CSV 경로>를 읽어서 각 컬럼이 어떤 kind로 분류되는지 출력하는
스크립트를 만들어줘. 오분류가 있으면 임계값 조정을 제안해줘.
```

## 5. Claude에게 시킬 때 주의할 점

`CLAUDE.md`의 **아키텍처 불변식 5가지**가 핵심입니다. Claude가 이걸 어기려 하면
멈추고 지적하세요. 특히 자주 나오는 실수:

| 증상 | 무엇이 잘못됐나 |
|---|---|
| 그래프에 노드를 추가하려 함 | skill로 만들어야 함 |
| `planner.py`에 skill 이름을 하드코딩 | `applies_when`으로 표현해야 함 |
| handler 안에 `if`로 검증 로직을 넣음 | `VerifiableClaim` + probe로 선언해야 함 |
| state에 새 필드를 추가 | board 슬롯으로 만들어야 함 |

`make test`가 통과하는지 매번 확인하세요. 22개 테스트가 불변식을 지킵니다.

## 6. 테스트를 믿을 때 주의

`tests/fixtures.py`의 `MockDeps`가 반환하는 값과 픽스처 데이터가 어긋나면
모든 작업이 probe에 막혀 **0건 처리인데 테스트가 통과**할 수 있습니다.
실제로 겪었습니다. 성능·확장성 단언에는 **처리 건수 단언을 반드시 함께** 두세요.

```python
# 나쁨 — 0 <= 4 로 통과함
assert waves <= 4

# 좋음
assert len(done) == n_cols, "처리 0건이면 파도 측정이 무의미하다"
assert waves <= expected + 1
```

## 7. 참고

- Claude Code 설치/설정: https://code.claude.com/docs/en/setup
- 문제 진단: `claude doctor`
