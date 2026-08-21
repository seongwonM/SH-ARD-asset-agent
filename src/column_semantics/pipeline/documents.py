"""실행 상태를 6개 문서로 나눠 담는다. 결과 파일 하나당 문서 하나다.

한 파일에 전부 담으면 "LLM이 주장한 것 / 데이터가 측정한 것 / 코드가 계획한 것"이
같은 트리 안에서 섞여, 나중에 결과를 분석할 때 어느 쪽 근거인지 매번 따져야 한다.
문서를 가르는 기준은 크기가 아니라 **출처**다.

    columns    컬럼 하나가 단계를 지나며 어떻게 바뀌었는지
    rulebase   룰베이스로 계산한 값 전부 - 프로파일, 관계 증거, probe 실측/불가 사유
    plan       무엇을 어떤 순서로 돌렸고, 계획이 어떻게 바뀌었는지
    table      테이블 단위 산출물 - 테이블 설명, 관계 분석, 묶어 본 결과, 검증
    llm_calls  모든 LLM 호출의 입력과 출력 원문
    lean       같은 입력으로 따로 받아둔 **최소 출력** (파이프라인은 읽지 않는다)

교차 참조는 id로만 한다(check_id, probe_id). 같은 값을 두 문서에 복사해 두면
한쪽만 고쳐질 때 어느 쪽이 맞는지 알 수 없게 된다.

`lean`이 다른 다섯과 갈라져 있는 이유도 출처다. 저건 이 실행의 산출물이 아니라
**산출물에 대한 측정**이다 - 위 다섯을 만든 것과 같은 payload로 최소 출력
프롬프트를 한 번 더 부른 결과이고, 어느 단계도 그 값을 읽지 않는다. 섞어두면
"파이프라인이 쓴 값"과 "비교하려고 받아둔 값"이 한 트리에 앉아, 나중에 분석하는
쪽이 매번 어느 쪽인지 따져야 한다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from column_semantics.core.history import ColumnHistory
from column_semantics.core.jsonx import clean_for_json
from column_semantics.core.lean_track import LeanTrack
from column_semantics.core.llm_log import LLMLog

PARTS = ["columns", "rulebase", "plan", "table", "llm_calls", "lean"]


def build_documents(
    *,
    meta: Dict[str, Any],
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    history: ColumnHistory,
    probe_log: List[Dict[str, Any]],
    planning: Dict[str, Any],
    validation_rounds: List[Dict[str, Any]],
    joint_findings: Optional[List[Dict[str, Any]]] = None,
    timeline_events: Optional[List[Dict[str, Any]]] = None,
    llm_log: Optional[LLMLog] = None,
    lean: Optional[LeanTrack] = None,
) -> Dict[str, Dict[str, Any]]:
    columns_order = evidence.get("table", {}).get("columns", [])

    documents = {
        "columns": _columns_doc(results, history, columns_order),
        "rulebase": _rulebase_doc(evidence, probe_log),
        "plan": _plan_doc(planning, timeline_events or []),
        "table": _table_doc(results, validation_rounds, joint_findings or []),
        "llm_calls": _llm_calls_doc(llm_log),
        "lean": _lean_doc(lean),
    }
    return {
        name: clean_for_json({"meta": {**meta, "part": name}, **body})
        for name, body in documents.items()
    }


def _columns_doc(
    results: Dict[str, Any], history: ColumnHistory, columns_order: List[str]
) -> Dict[str, Any]:
    """컬럼별 최종 상태 + 거기까지 온 단계들.

    `final`은 편의를 위한 요약이고, 진짜 내용은 `stages`다 - 어떤 단계가
    무엇을 바꿨는지(before/after/changed)가 거기 남는다.
    """
    interpretation = (results.get("column_interpretation") or {}).get("columns", {}) or {}
    validated = (results.get("semantic_validation") or {}).get("validated_columns", {}) or {}
    stages = history.columns(columns_order)

    return {
        "columns": {
            col: {
                "final": {
                    # 타입은 해석 안에 있다(같은 호출이 함께 낸다). 여기서 한 번 더
                    # 꺼내 두는 것은 읽는 쪽 편의고, 값은 해석의 것 그대로다.
                    "semantic_type": (interpretation.get(col) or {}).get("semantic_type")
                    if isinstance(interpretation.get(col), dict)
                    else None,
                    "interpretation": interpretation.get(col),
                    "validated": validated.get(col),
                },
                "stages": stages.get(col, []),
            }
            for col in columns_order
        }
    }


def _rulebase_doc(evidence: Dict[str, Any], probe_log: List[Dict[str, Any]]) -> Dict[str, Any]:
    """데이터에서 직접 계산한 값만. LLM이 쓴 문장은 여기 들어오지 않는다.

    `probes`에는 재본 것과 재보지 못한 것이 같이 들어간다. 후자는 `observed`가
    null이고 `not_evaluable`에 이유가 있다 - 없는 컬럼을 가리켰는지, 표본이
    모자랐는지, 식이 틀렸는지는 프롬프트를 고칠 때 쓰는 정보다.
    """
    return {
        "table": evidence.get("table", {}),
        "column_profiles": evidence.get("column_profiles", {}),
        "relation_evidence": evidence.get("relation_evidence", {}),
        "grain_candidates": evidence.get("grain_candidates", []),
        "probes": probe_log,
    }


def _plan_doc(planning: Dict[str, Any], timeline_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """계획의 전 과정. LLM 원출력(raw)과 코드가 정제한 결과를 나란히 둔다.

    gap_rounds에는 라운드별로 계획에 관한 것만 남는다: 누구를 후보로 봤고(`considered`),
    누가 게이트를 넘었고(`flagged`), planner가 무엇을 내놨고(원출력), 무엇이
    실행됐고, 무엇이 왜 버려졌고, 어떤 컬럼이 실제로 바뀌었는지. 버린 것을 남기는
    이유는, planner가 무엇을 하려 했는지가 사라지면 프롬프트를 고칠 근거도
    사라지기 때문이다. planner가 적은 근거(cites/reason)는 검증하지 않고 그대로 싣는다.

    게이트는 규칙이라(`gate: "domain_gap"`) 판정 근거를 여기 옮겨 적지 않는다 -
    `considered`와 컬럼 문서의 해석만 있으면 누가 왜 넘어갔는지 그대로 재현된다.
    """
    return {
        "first_pass": planning.get("first_pass", {}),
        "gap_rounds": planning.get("gap_rounds", []),
        "replans": planning.get("replans", []),
        "execution": timeline_events,
    }


def _table_doc(
    results: Dict[str, Any],
    validation_rounds: List[Dict[str, Any]],
    joint_findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """테이블 단위 산출물. 검증은 라운드별로 전부 남긴다 - 최종 상태만 남기면
    "무엇이 지적됐다가 해소됐는지"가 사라진다.

    `joint_findings`는 보완 단계에서 컬럼 여러 개를 묶어 본 결과다. 관계는 컬럼
    하나에 속한 값이 아니라서 컬럼 문서에 넣으면 그룹 크기만큼 복사된다 -
    여기 한 벌만 두고, 컬럼 이력은 `with_columns`로 그 묶음을 가리킨다.
    """
    final = results.get("semantic_validation") or {}
    return {
        "table_context": results.get("table_context"),
        "relation_analysis": results.get("relation_analysis"),
        "joint_findings": joint_findings,
        "validation": {
            "final_status": final.get("overall_status"),
            "rounds": validation_rounds,
        },
    }


def _llm_calls_doc(llm_log: Optional[LLMLog]) -> Dict[str, Any]:
    """호출 원문. system 프롬프트는 skill당 한 벌만 두고 호출은 prompt_ref로 가리킨다."""
    if llm_log is None:
        return {"prompts": {}, "calls": []}
    return {"prompts": llm_log.prompts(), "calls": llm_log.calls()}


def _lean_doc(lean: Optional[LeanTrack]) -> Dict[str, Any]:
    """단계별 최소 출력. 켜지 않았으면 `enabled: false`로 비어 있다.

    `stages`는 짝을 맞춰 보기 좋게 {단계: {대상: 출력}}으로 접어둔 것이고,
    `entries`는 시간순 원본이다(수정 라운드에서 같은 대상이 두 번 나올 수 있는데,
    접힌 쪽은 마지막만 남는다).

    비용은 여기 적지 않는다 - 최소 출력 호출도 다른 호출과 똑같이 `llm_calls`에
    들어가므로(prompt_ref가 `lean_*`), 토큰과 시간은 거기서 센다.
    """
    if lean is None:
        return {"enabled": False, "stages": {}, "entries": []}
    return {"enabled": True, "stages": lean.by_stage(), "entries": lean.entries()}


__all__ = ["PARTS", "build_documents"]
