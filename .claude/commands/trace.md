---
description: 에이전트를 실행하고 skill 선택 궤적을 분석한다
argument-hint: [분석하고 싶은 것]
---

`python -m pytest tests/test_agent.py -q` 후
`PYTHONPATH=src:tests python tests/test_agent.py`를 실행해 궤적을 출력한다.

다음을 확인하고 보고한다. 관심사: $ARGUMENTS

1. **선택 근거** — 각 plan 단계의 note에 적힌 score와 이유가 납득 가능한가
2. **배치 효율** — 컬럼 작업이 몇 파도로 처리됐는가. 파도가 컬럼 수에 비례하면 배치가 깨진 것
3. **재시도** — `tries>1`인 작업의 `trail`을 보고 무엇이 반증됐는지 확인
4. **검증률** — verification 리포트의 `coverage`. 낮으면 probe가 붙지 않은 주장이 많다는 뜻
5. **격리** — `blocked` 목록. 비어 있지 않으면 왜 실패했는지 원인 확인

문제를 발견하면 **어느 계층의 문제인지** 먼저 판단한다.
- 잘못된 skill이 선택됨 → `planner.py` 점수 또는 `SKILL.md`의 `applies_when`
- 잘못된 출력이 통과됨 → probe 부재 또는 `guards.py`
- 정상 출력이 막힘 → 가드 false positive (가장 위험. 전부 막히면 가드를 끈 것과 같다)
