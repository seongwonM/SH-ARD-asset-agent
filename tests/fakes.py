"""가짜 LLM. 파이프라인 전 구간을 서버 없이 돌리기 위한 것.

adapters.LLMClient 프로토콜만 만족하면 되므로 openai 패키지도 필요 없다.
응답은 label로 분기한다 - label 형식이 바뀌면 여기서 바로 드러난다.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional


class FakeLLM:
    model = "fake-model"

    def __init__(self, refute_first_validation: bool = True, llm_log: Any = None):
        self.calls: List[Dict[str, Any]] = []
        self.refute_first_validation = refute_first_validation
        # 진짜 어댑터는 호출 원문을 LLMLog에 남긴다(그 동작 자체는
        # tests/test_llm_log.py가 검증한다). 여기서 log를 받는 것은 app -> 문서 ->
        # 파일로 이어지는 배선이 실제로 이어져 있는지 확인하기 위한 최소한이다.
        self.llm_log = llm_log
        self._validation_rounds = 0
        self._lock = threading.Lock()

    # -- LLMClient -----------------------------------------------------

    def complete_json(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        *,
        label: str = "",
        max_retries: int = 1,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.calls.append(
                {
                    "label": label,
                    "prompt": system_prompt,
                    "payload": payload,
                    "context": context or {},
                }
            )
        head = label.split(":", 1)[0]
        handler = getattr(self, f"_on_{head}", None)
        if handler is None:
            raise AssertionError(f"FakeLLM이 모르는 label: {label}")
        try:
            result = handler(label, payload)
        except Exception as e:
            # 진짜 어댑터는 실패한 호출도 기록하고 나서 올려보낸다. 가짜가 그냥
            # 던져버리면 "죽은 호출이 파일에 남는가"를 테스트로 확인할 수 없다.
            self._record(label, system_prompt, payload, context, status="error",
                         response_text=None, response=None, error=f"{type(e).__name__}: {e}")
            raise
        self._record(label, system_prompt, payload, context, status="ok",
                     response_text=json.dumps(result, ensure_ascii=False), response=result)
        return result

    def _record(self, label, system_prompt, payload, context, **fields) -> None:
        if self.llm_log is None:
            return
        self.llm_log.add(
            prompt_ref=self.llm_log.register_prompt(
                str((context or {}).get("name") or label), system_prompt
            ),
            payload=payload,
            # 진짜 어댑터는 label도 기록에 넣는다 - 문서 모양이 어긋나면 안 된다.
            context={"label": label, **(context or {})},
            attempt=1,
            model=self.model,
            **fields,
        )

    # -- 편의 ----------------------------------------------------------

    def labels(self) -> List[str]:
        return [c["label"] for c in self.calls]

    def context_for(self, label: str) -> Dict[str, Any]:
        for call in self.calls:
            if call["label"] == label:
                return call["context"]
        raise KeyError(label)

    def payload_for(self, label: str) -> Dict[str, Any]:
        for call in self.calls:
            if call["label"] == label:
                return call["payload"]
        raise KeyError(label)

    # -- skill별 응답 ----------------------------------------------------

    def _on_column_interpretation(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        column = payload["target_column"]
        typed = {"semantic_type": {"type": "identifier", "confidence": 0.6}}
        if payload.get("revision_feedback") is not None:
            return {**typed, "status": "resolved", "selected_meaning": f"{column}의 의미(재해석)"}
        # power_value만 애매하게 두어 gap skill이 붙는 경로를 만든다. 보완으로
        # 넘어가는 조건은 domain_gap 유무 하나라, 그것도 같이 남긴다.
        if column == "power_value":
            return {
                **typed,
                "status": "ambiguous",
                "candidates": ["출력", "소비전력"],
                "domain_gap": {
                    "missing": "출력인지 소비전력인지",
                    "why": "이름과 값 범위만으로는 어느 쪽인지 정해지지 않는다",
                    "would_resolve": ["설비 사양서"],
                },
            }
        # status는 resolved인데 도메인은 못 밝힌 컬럼. 두 축이 별개라는 걸 보여준다.
        if column == "status_code":
            return {
                **typed,
                "status": "resolved",
                "selected_meaning": "0/1 두 값만 갖는 상태 플래그",
                "domain_gap": {
                    "missing": "0과 1이 각각 어느 상태를 뜻하는지",
                    "why": "값이 코드일 뿐이고 이름 밖에 단서가 없다",
                    "would_resolve": ["공통코드 표", "컬럼 코멘트"],
                },
            }
        return {
            **typed,
            "status": "resolved",
            "selected_meaning": f"{column}의 의미",
            "domain_gap": None,
        }

    def _on_relation_analysis(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "relations": [],
            "revised_columns": {},
            "groups": [{"columns": ["power_value", "power_limit"], "kind": "measure_limit"}],
        }

    def _on_gap_planner(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "actions": [
                {
                    "action": "reconsider_ambiguous",
                    "columns": ["power_value"],
                    "reason": "후보가 둘이라 확정되지 않음",
                    "cites": [],
                },
                # 아래는 전부 정제 단계에서 버려져야 한다.
                {"action": "reconsider_ambiguous", "columns": ["없는컬럼"], "reason": "x"},
                {"action": "존재하지_않는_skill", "columns": ["run_id"], "reason": "x"},
                {"action": "explain_sparsity", "columns": ["run_id"], "reason": "게이트가 안 넘긴 컬럼"},
                {"action": "joint_interpretation", "columns": ["power_value"], "reason": "혼자 묶기"},
            ],
            "skipped": [],
        }

    def _on_reconsider_ambiguous(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # domain_gap을 명시적으로 null로 닫는다. 이게 안 되면 한 번 걸린 컬럼이
        # 라운드마다 다시 걸린다.
        return {"status": "resolved", "selected_meaning": "설비 출력값(W)", "domain_gap": None}

    def _on_joint_interpretation(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "relationship": "power_value는 power_limit에 대한 측정값이다",
            "columns": {
                c: {"status": "resolved", "selected_meaning": f"{c}(그룹 해석)"}
                for c in payload["columns"]
            },
            "probe": None,
        }

    def _on_semantic_validation(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 수정 라운드에서는 revision_feedback이 채워져 들어온다 - 그걸로 회차를 구분한다.
        first_round = payload.get("revision_feedback") is None
        columns = list(payload["column_profiles"])
        if "power_value" in columns and "power_limit" in columns and self.refute_first_validation and first_round:
            return {
                "overall_status": "pass",  # LLM은 통과라고 주장한다
                "checks": [
                    {
                        "hypothesis": "power_value는 power_limit 이하다",
                        "columns": ["power_value", "power_limit"],
                        "status": "pass",
                        "probe": {
                            "expression": "v <= lim",
                            "columns": {"v": "power_value", "lim": "power_limit"},
                        },
                    }
                ],
                "revision_requests": [{"column": "power_value", "reason": "한계 초과 값 존재"}],
                "validated_columns": {c: {"status": "ok"} for c in columns},
            }
        return {
            "overall_status": "pass",
            "checks": [],
            "revision_requests": [],
            "validated_columns": {c: {"status": "ok"} for c in columns},
        }

    def _on_replan(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "reason": "probe가 한계 초과를 확인했으므로 해당 컬럼만 다시 해석한다",
            "steps": [
                {"stage": "column_interpretation", "goal": "재해석", "focus": ["power_value"]},
                # table_context는 계획에서 제거되어야 한다(재계획 후 항상 자동 생성).
                {"stage": "table_context", "goal": "무시되어야 함", "focus": []},
            ],
        }

    def _on_table_context(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "table_summary": "설비 실행 로그",
            "grain": "run_id 1건 = 실행 1회",
        }

    # -- 단계별 최소 출력 -------------------------------------------------
    # 같은 payload를 받아 최소 필드만 낸다. 파이프라인이 이 값을 절대 읽지 않는
    # 것까지가 계약이라, 여기 응답은 본 단계의 응답과 일부러 다르게 써 둔다 -
    # 어딘가에서 새어 들어오면 결과가 눈에 띄게 어긋난다.

    def _on_lean_column_interpretation(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "meaning": f"{payload['target_column']}의 의미(최소)",
            "unit": None,
            "unknown": None,
        }

    def _on_lean_relation_analysis(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"revised_columns": {}}

    def _on_lean_semantic_validation(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"wrong_meanings": []}

    def _on_lean_table_context(self, label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"asset_context": "설비 실행 로그(최소)", "row_grain": "실행 1회"}
