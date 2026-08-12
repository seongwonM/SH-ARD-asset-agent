---
description: 새 probe 종류를 추가한다
argument-hint: <probe 이름> <검증하려는 주장의 종류>
---

`$1` probe를 추가한다. 검증 대상: $2

## 원칙

probe는 **반증**하는 도구다. 통과가 참을 증명하지 않고, 실패가 거짓을 증명한다.
"이 주장이 틀렸다면 데이터에서 무엇이 보일까"를 먼저 정하고 그것을 검사한다.

## 절차

1. `src/agent/probes.py`의 `ProbeKind`에 항목 추가
2. `_<name>(req, df) -> ProbeResult` 구현
   - `detail`에 **실측값을 반드시 포함**한다. 이 문자열이 그대로 LLM 재시도 힌트가 된다.
     "유일하지 않다"가 아니라 "실제 유일성 비율은 0.42이다(기준 0.99)"로 쓴다.
   - 데이터를 못 읽는 상황은 `passed=False`가 아니라 `error`에 담는다.
     probe 실행 실패는 주장을 반증하지 못한다.
3. `_DISPATCH`에 등록
4. `tests/test_probes.py`에 통과/반증 양쪽 케이스 추가
5. 이 probe를 쓸 skill의 handler에서 `VerifiableClaim`으로 첨부
