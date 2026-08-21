"""한 번의 LLM 호출 = 그에 맞는 payload를 만들어 넘기는 일. 그 조립 규칙만 여기 있다.

**여기 있는 것은 payload 조립 규칙뿐이다.** 실행 순서/병렬화/재시도 라운드는
orchestrator가, JSON 파싱/레이트리밋/호출 기록은 LLM 어댑터가 맡는다.

프롬프트는 두 곳에서 온다 - 코드가 순서대로 돌리는 고정 단계(`stages`)와,
gap_planner가 컬럼별로 붙이는 보완 skill(`skills`). 둘을 한 라이브러리로 합치지
않는 이유는, 합치는 순간 "이 프롬프트가 언제 도는가"가 이름만 봐서는 알 수 없게
되기 때문이다. 호출 기록에도 `kind`로 남는다.

payload를 좁히는 게 이 클래스의 핵심이다. 테이블 전체를 통째로 넣으면 컬럼이
늘수록 무관한 정보가 판단을 흐리고 토큰만 커진다 - 컬럼 단위는 그 컬럼의
프로파일만, 그룹 단위는 그 그룹에 속한 컬럼의 증거만 본다.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from column_semantics.adapters.llm import LLMClient
from column_semantics.adapters.prompts import PromptLibrary
from column_semantics.core.lean_track import LeanTrack
from column_semantics.pipeline.plan import (
    GAP_SKILLS,
    LEAN_STAGES,
    REPLAN_STAGES,
    sanitize_plan,
)


class StageRunner:
    def __init__(
        self,
        stages: PromptLibrary,
        skills: PromptLibrary,
        llm: LLMClient,
        lean: Optional[LeanTrack] = None,
    ):
        self.stages = stages
        self.skills = skills
        self.llm = llm
        # 주어졌을 때만 단계마다 최소 출력을 한 번 더 받는다(호출이 두 배가 되므로
        # 기본은 꺼져 있다). 받아둔 값은 문서에만 남고 실행에는 쓰이지 않는다.
        self.lean = lean
        self._stage: Dict[str, Any] = {}

    @contextmanager
    def stage(self, **fields: Any) -> Iterator[None]:
        """지금 어느 단계를 도는 중인지 표시한다(기록 전용, 실행에는 영향 없다).

        병렬 호출은 항상 한 단계 안에서만 일어나고 단계 전환은 그 병렬 블록이
        모두 끝난 뒤에 orchestrator가 단독으로 한다. 그래서 쓰는 쪽은 언제나
        하나, 읽는 쪽만 여럿이라 락이 필요 없다.
        """
        previous = self._stage
        self._stage = {**previous, **fields}
        try:
            yield
        finally:
            self._stage = previous

    def _call(
        self,
        name: str,
        library: PromptLibrary,
        kind: str,
        payload: Dict[str, Any],
        label: str,
        **context: Any,
    ) -> Dict[str, Any]:
        return self.llm.complete_json(
            library.prompt(name),
            payload,
            label=label,
            context={**self._stage, "name": name, "kind": kind, **context},
        )

    def _call_stage(self, name: str, payload: Dict[str, Any], label: str, **context: Any):
        return self._call(name, self.stages, "stage", payload, label, **context)

    def _call_skill(self, name: str, payload: Dict[str, Any], label: str, **context: Any):
        return self._call(name, self.skills, "skill", payload, label, **context)

    def _lean(self, stage: str, target: str, payload: Dict[str, Any]) -> None:
        """같은 payload로 최소 출력을 한 번 더 받는다. 결과는 기록에만 남는다.

        비교가 성립하려면 입력이 같아야 하므로 payload를 손대지 않는다. 그리고
        여기서 죽어도 파이프라인은 계속 간다 - 이건 측정이지 산출물이 아니라서,
        측정 때문에 본 실행이 무너지면 앞뒤가 바뀐다. 실패는 기록에 남는다.
        """
        if self.lean is None:
            return
        prompt = LEAN_STAGES.get(stage)
        if prompt is None:
            return
        try:
            output = self._call(
                prompt, self.stages, "lean", payload, label=f"{prompt}:{target}", target=target
            )
            self.lean.record(stage, target, output=output)
        except Exception as e:  # noqa: BLE001 - 측정 실패가 실행을 죽이면 안 된다
            self.lean.record(stage, target, error=f"{type(e).__name__}: {e}")

    # -- 계획 -----------------------------------------------------------

    def plan(
        self,
        evidence: Dict[str, Any],
        previous_results: Dict[str, Any],
        validation_feedback: Dict[str, Any],
    ) -> Dict[str, Any]:
        """검증 실패 후 재계획. 1차 pass는 고정 순서라 이 함수를 거치지 않는다 -
        여기는 항상 '지금까지의 결과 + 검증 실패 이유를 보고 다음에 뭘 해야 하는지'다.

        {"raw": LLM 원출력, "plan": 정제된 계획}을 돌려준다."""
        payload = {
            "objective": (
                "semantic_validation이 지적한 모순을 해소하도록, 필요한 단계만 다시 실행하는 계획을 세운다."
            ),
            "available_stages": REPLAN_STAGES,
            "table_summary": evidence["table"],
            "grain_candidates": evidence.get("grain_candidates", []),
            "has_pairwise_evidence": bool(evidence.get("relation_evidence", {}).get("pairwise")),
            "previous_results": previous_results,
            "validation_feedback": validation_feedback,
        }
        raw = self._call(
            "planner", self.stages, "planner", payload, label="replan"
        )
        # 정제는 원본을 제자리에서 고친다. "LLM이 뭘 냈고 코드가 뭘 걸러냈는지"를
        # 나중에 대조하려면 고치기 전 사본이 있어야 한다.
        return {"raw": copy.deepcopy(raw), "plan": sanitize_plan(raw)}

    def plan_gaps(
        self,
        flagged: List[str],
        evidence: Dict[str, Any],
        results: Dict[str, Any],
    ) -> Dict[str, Any]:
        """domain_gap이 남은 컬럼들을 한자리에 놓고 무엇을 할지 정한다.

        여러 컬럼을 묶어 봐야 한다는 판단은 컬럼 하나만 보는 호출이 할 수 없다.
        그 판단이 가능한 유일한 지점이라 여기서만 여러 컬럼을 한 payload에 넣는다 -
        대신 넘어온 컬럼과 그 짝만 넣지, 테이블 전체를 넣지 않는다.

        `gap`은 해석이 스스로 적은 `domain_gap`을 그대로 넘긴다. 예전에는 컬럼별
        검토 호출이 쓴 문장을 넣었는데, 같은 재료로 같은 모델이 두 번째로 쓴
        문장이었다 - 무엇을 모르는지는 해석이 이미 필드로 적어두고 있었다.
        """
        column_interp = (results.get("column_interpretation") or {}).get("columns", {})
        flagged_columns = [
            {
                "name": col,
                "column_profile": evidence["column_profiles"].get(col),
                "interpretation": column_interp.get(col),
                "domain_gap": (column_interp.get(col) or {}).get("domain_gap"),
            }
            for col in flagged
        ]
        payload = {
            "flagged_columns": flagged_columns,
            "pairwise_evidence": _pairwise_touching(evidence, flagged),
            "all_column_names": evidence["table"]["columns"],
            "available_actions": GAP_SKILLS,
        }
        raw = self._call("gap_planner", self.stages, "planner", payload, label="gap_planner")
        return {"raw": copy.deepcopy(raw), "actions": raw.get("actions"), "skipped": raw.get("skipped")}

    # -- 컬럼/그룹 단위 고정 단계 ----------------------------------------

    def interpret_column(
        self,
        column: str,
        evidence: Dict[str, Any],
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {
            "table": evidence["table"],
            "target_column": column,
            "column_profile": evidence["column_profiles"][column],
            "raw_other_column_names": evidence["table"]["columns"],
            "revision_feedback": revision_feedback,
        }
        result = self._call_stage(
            "column_interpretation",
            payload,
            label=f"column_interpretation:{column}",
            column=column,
        )
        self._lean("column_interpretation", column, payload)
        return result

    def validate_group(
        self,
        columns: List[str],
        evidence: Dict[str, Any],
        results: Dict[str, Any],
        revision_feedback: Optional[Dict[str, Any]] = None,
        group_label: str = "",
    ) -> Dict[str, Any]:
        col_set = set(columns)
        column_profiles = {c: evidence["column_profiles"][c] for c in columns}
        pairwise = [
            p
            for p in evidence.get("relation_evidence", {}).get("pairwise", [])
            if set(p.get("columns", [])) <= col_set
        ]
        grain_candidates = [
            g for g in evidence.get("grain_candidates", []) if set(g.get("columns", [])) <= col_set
        ]
        semantic_type_cols = semantic_types(results)
        column_interp_cols = (results.get("column_interpretation") or {}).get("columns", {})
        relation_analysis = results.get("relation_analysis") or {}
        relation_analysis_scoped = {
            "relations": [
                r
                for r in relation_analysis.get("relations", [])
                if set(r.get("columns", [])) <= col_set
            ],
            "revised_columns": {
                c: v
                for c, v in (relation_analysis.get("revised_columns") or {}).items()
                if c in col_set
            },
            "groups": [
                g for g in relation_analysis.get("groups", []) if set(g.get("columns", [])) <= col_set
            ],
        }

        payload = {
            "table": evidence["table"],
            "column_profiles": column_profiles,
            "relation_evidence": {"pairwise": pairwise},
            "grain_candidates": grain_candidates,
            "semantic_type": {
                "columns": {c: semantic_type_cols.get(c) for c in columns if c in semantic_type_cols}
            },
            "column_interpretation": {
                "columns": {c: column_interp_cols.get(c) for c in columns if c in column_interp_cols}
            },
            "relation_analysis": relation_analysis_scoped,
            "revision_feedback": revision_feedback,
        }
        target = group_label or "+".join(columns)
        result = self._call_stage(
            "semantic_validation",
            payload,
            label=f"semantic_validation:{target}",
            group=group_label,
            columns=list(columns),
        )
        self._lean("semantic_validation", target, payload)
        return result

    # -- 테이블 단위 고정 단계 -------------------------------------------

    def run_stage(
        self,
        stage: str,
        evidence: Dict[str, Any],
        results: Dict[str, Any],
        focus: Optional[List[str]] = None,
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """단일 LLM 호출로 끝나는 고정 단계 전용. column_interpretation은 컬럼별
        병렬이, semantic_validation은 그룹별 병렬이 각각 따로 처리한다."""

        if stage == "relation_analysis":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "relation_evidence": evidence["relation_evidence"],
                "grain_candidates": evidence["grain_candidates"],
                "semantic_type": semantic_types(results),
                "column_interpretation": results.get("column_interpretation"),
                "previous_relation_analysis": results.get("relation_analysis"),
                "revision_feedback": revision_feedback,
                "focus": focus or [],
            }

        elif stage == "table_context":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "grain_candidates": evidence["grain_candidates"],
                "semantic_type": semantic_types(results),
                "column_interpretation": results.get("column_interpretation"),
                "relation_analysis": results.get("relation_analysis"),
                "semantic_validation": results.get("semantic_validation"),
                "focus": focus or [],
            }

        else:
            raise ValueError(f"run_stage는 {stage}을 처리하지 않는다 (전용 메서드를 쓸 것)")

        result = self._call_stage(stage, payload, label=stage)
        self._lean(stage, "table", payload)
        return result

    # -- 보완 skill ------------------------------------------------------

    def run_gap_skill(
        self,
        skill_name: str,
        columns: List[str],
        evidence: Dict[str, Any],
        results: Dict[str, Any],
        reason: str = "",
    ) -> Dict[str, Any]:
        """gap_planner가 배정했을 때만 불린다. 컬럼 하나짜리 보완과 여러 컬럼을
        같이 보는 보완이 같은 자리를 쓴다 - 배정 단위가 컬럼 집합이라서다."""
        column_interp = (results.get("column_interpretation") or {}).get("columns", {})
        semantic_type = semantic_types(results)

        if skill_name == "joint_interpretation":
            payload = {
                "columns": columns,
                "column_profiles": {c: evidence["column_profiles"][c] for c in columns},
                "interpretations": {c: column_interp.get(c) for c in columns},
                "pairwise_evidence": _pairwise_within(evidence, columns),
                "reason": reason,
            }
            return self._call_skill(
                skill_name,
                payload,
                label=f"{skill_name}:{'+'.join(columns)}",
                columns=list(columns),
            )

        column = columns[0]
        if skill_name == "reconsider_ambiguous":
            other_resolved = {
                c: v.get("selected_meaning")
                for c, v in column_interp.items()
                if c != column and v.get("status") == "resolved"
            }
            payload = {
                "target_column": column,
                "column_profile": evidence["column_profiles"][column],
                "current_interpretation": column_interp.get(column),
                "other_resolved_columns": other_resolved,
                "reason": reason,
            }
        elif skill_name == "explain_sparsity":
            payload = {
                "target_column": column,
                "column_profile": evidence["column_profiles"][column],
                "current_interpretation": column_interp.get(column),
                "table_summary": evidence["table"],
                "reason": reason,
            }
        elif skill_name == "reconcile_type_meaning":
            payload = {
                "target_column": column,
                "column_profile": evidence["column_profiles"][column],
                "semantic_type": semantic_type.get(column),
                "current_interpretation": column_interp.get(column),
                "reason": reason,
            }
        else:
            raise ValueError(f"알 수 없는 보완 skill: {skill_name}")

        return self._call_skill(
            skill_name, payload, label=f"{skill_name}:{column}", column=column
        )


def semantic_types(results: Dict[str, Any]) -> Dict[str, Any]:
    """컬럼별 semantic_type. 이제 해석 결과 안에 들어 있다.

    예전에는 테이블 단위 semantic_type 단계가 따로 있었는데, 그 호출은 전 컬럼
    프로파일을 payload로 받으면서 정작 "다른 컬럼을 근거로 삼지 말라"는 제약을
    걸고 있었다 - 쓰지 말라는 맥락의 비용만 냈고, 같은 컬럼에 대해 타입과 의미가
    어긋나는 일까지 만들었다. 지금은 한 번의 컬럼 해석이 둘 다 낸다.
    """
    columns = (results.get("column_interpretation") or {}).get("columns", {}) or {}
    return {
        col: value.get("semantic_type")
        for col, value in columns.items()
        if isinstance(value, dict) and value.get("semantic_type") is not None
    }


def _pairwise_touching(evidence: Dict[str, Any], columns: List[str]) -> List[Dict[str, Any]]:
    """지정한 컬럼이 한쪽에라도 낀 pairwise 증거만."""
    targets = set(columns)
    return [
        p
        for p in evidence.get("relation_evidence", {}).get("pairwise", [])
        if targets & set(p.get("columns", []))
    ]


def _pairwise_within(evidence: Dict[str, Any], columns: List[str]) -> List[Dict[str, Any]]:
    """양쪽 모두 그 그룹 안에 있는 pairwise 증거만."""
    targets = set(columns)
    return [
        p
        for p in evidence.get("relation_evidence", {}).get("pairwise", [])
        if set(p.get("columns", [])) <= targets
    ]
