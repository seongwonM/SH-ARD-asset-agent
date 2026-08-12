"""
기존 `asset_agent.skills.structured_asset.TableAssetContextBuilder` 계약 호환 어댑터.

왜 필요한가
-----------
기존 레포에는 `examples/run_robustness_test.py`(데이터셋 x column_descriptions
유무 x 모델 3종 x 20회 반복 = 최대 1440회 배치)와 `analyze_robustness_test.py`가
있고, 이들이 builder의 **출력 JSON 모양**에 강하게 결합돼 있다.
결과 JSONL은 PVC에 누적되며 재실행 시 이어달리기를 한다.

즉 출력 스키마를 바꾸면 **이미 쌓인 결과와 신규 결과를 비교할 수 없게 되고**,
강건성 테스트의 목적(모델별/반복별 차이 관찰) 자체가 무너진다.

그래서 내부 엔진만 plan/act 에이전트로 바꾸고, 이 어댑터가 옛 계약을 그대로
재현한다. `run_robustness_test.py`와 `analyze_robustness_test.py`는 한 줄도
고치지 않는다.

새 정보(probe 검증 결과, skill 선택 궤적)는 옛 키를 건드리지 않고
`asset_context.verification` / `trace`로 **추가**만 한다. 기존 집계 스크립트는
모르는 키를 무시하므로 안전하다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from .contract import SkillDeps, Slot
from .graph import build_agent
from .state import new_state

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class TableAssetContextBuilder:
    """
    옛 계약 유지용 파사드.

    옛 시그니처: build(tabular_data, asset_name, source_description, column_descriptions)
    옛 반환 키 : input / tabular_profile_output / column_context_output /
                initial_asset_context_output / asset_context / issues / performance
    """

    def __init__(
        self,
        client: Any = None,
        config: Any = None,
        deps: SkillDeps | None = None,
        skills_dir: Path | None = None,
        max_iterations: int = 24,
        max_llm_calls: int = 300,
    ) -> None:
        self.config = config
        self.skills_dir = skills_dir or SKILLS_DIR
        self.max_iterations = max_iterations
        self.max_llm_calls = max_llm_calls

        if deps is not None:
            self.deps = deps
        else:
            from .llm import RuntimeDeps

            # 옛 코드는 openai.OpenAI 또는 LLMClient를 넘긴다.
            # RuntimeDeps가 둘 다 받도록 raw_client로 위임한다.
            self.deps = RuntimeDeps(raw_client=client, config=config)

        self._agent = build_agent(self.skills_dir, self.deps)

    # ------------------------------------------------------------------
    def build(
        self,
        tabular_data,
        asset_name: str,
        source_description: str | None = None,
        column_descriptions: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        started = time.time()
        ref = f"runtime://{asset_name}"
        register = getattr(self.deps, "register_frame", None)
        if register is not None:
            register(ref, tabular_data)

        state = new_state(
            asset_id=asset_name,
            asset_name=asset_name,
            data_ref=ref,
            source_description=source_description or "",
            column_descriptions=column_descriptions or {},
            max_iterations=self.max_iterations,
            max_llm_calls=self.max_llm_calls,
        )

        try:
            final = _run_sync(self._agent.ainvoke(state, {"recursion_limit": 400}))
        except Exception as exc:  # noqa: BLE001
            # 옛 계약은 예외를 던지지 않고 issues에 기록했다. 배치 스크립트가
            # 회차마다 try/except를 하긴 하지만, 여기서 삼켜야 부분 결과라도 남는다.
            logger.exception("에이전트 실행 실패")
            return _empty_result(
                asset_name, source_description, column_descriptions, started,
                [{"stage": "agent", "error_type": type(exc).__name__, "message": str(exc)}],
            )

        return _to_legacy_shape(
            final, asset_name, source_description, column_descriptions, started, self.deps
        )


# ---------------------------------------------------------------------------
# 변환
# ---------------------------------------------------------------------------


def _to_legacy_shape(
    final, asset_name, source_description, column_descriptions, started, deps
) -> Dict[str, Any]:
    values = (final.get("board") or {}).get("values", {})
    keyed = (final.get("board") or {}).get("keyed", {})

    profile = values.get(Slot.TABLE_PROFILE.value) or {}
    ctx = values.get(Slot.ASSET_CONTEXT.value) or {}
    verification = values.get(Slot.VERIFICATION.value) or {}
    semantics: Dict[str, Any] = keyed.get(Slot.COLUMN_SEMANTICS.value, {})

    # --- tabular_profile_output: 옛 구조가 요구한 2개 문자열 -----------------
    tabular_profile_output = (
        {
            "profile_summary": _render_profile_summary(profile),
            "coverage_summary": _render_coverage_summary(profile, semantics),
        }
        if profile
        else None
    )

    # --- column_context_output ---------------------------------------------
    column_context_output = {
        "column_context_summary": "\n".join(
            f"**{name}**: {info.get('meaning', '')}" for name, info in semantics.items() if info.get("meaning")
        )
    }

    # --- initial_asset_context_output --------------------------------------
    initial_asset_context_output = {"summary": values.get(Slot.SUMMARY.value)}

    # --- asset_context ------------------------------------------------------
    asset_context = None
    if ctx:
        asset_context = {
            "asset_context_details": {
                "summary": ctx.get("summary", ""),
                "key_points": ctx.get("key_points", []),
                "use_cases": ctx.get("use_cases", []),
                "related_concepts": ctx.get("related_concepts", []),
                "keywords": ctx.get("keywords", ctx.get("search_terms", [])),
                "additional_context": ctx.get("additional_context", []),
            },
            "search_text": ctx.get("search_text", ""),
            # --- 신규 필드(옛 집계 스크립트는 무시한다) ---
            "verification": ctx.get("verification", verification and {
                "status": verification.get("status"),
                "verified": len(verification.get("verified", [])),
                "refuted": [e["statement"] for e in verification.get("refuted", [])],
                "unverified_count": len(verification.get("unverified", [])),
                "probe_coverage": verification.get("coverage", 0.0),
            }),
            "columns": ctx.get("columns", []),
            "grain": ctx.get("grain", ""),
            "grain_keys": ctx.get("grain_keys", []),
            "linkage": ctx.get("linkage", []),
            "coverage": ctx.get("coverage", ""),
            "constraints": ctx.get("constraints", {}),
            "compliance": ctx.get("compliance", {}),
            "glossary": ctx.get("glossary", {}),
        }

    # --- issues: 실패 이력 + 격리된 조합 -------------------------------------
    issues: List[Dict[str, Any]] = []
    for h in final.get("history", []):
        if h.get("phase") == "plan" or h.get("ok", True):
            continue
        issues.append(
            {
                "stage": h.get("skill", "unknown"),
                "error_type": None,
                "message": f"{h.get('target') or 'table'}: {h.get('reason', '')}"[:500],
            }
        )
    for entry in verification.get("refuted", []):
        issues.append(
            {"stage": "verify-context", "error_type": None,
             "message": f"반증된 주장: {entry.get('statement')} — {entry.get('detail')}"[:500]}
        )
    for c in verification.get("contradictions", []):
        issues.append({"stage": "verify-context", "error_type": None, "message": c[:500]})

    stop = final.get("stop_reason", "")
    if stop and stop != "goal_reached":
        issues.append({"stage": "planner", "error_type": None, "message": f"종료 사유: {stop}"})

    # --- performance --------------------------------------------------------
    budget = final.get("budget") or {}
    stats = {}
    get_stats = getattr(deps, "get_stats", None)
    if callable(get_stats):
        try:
            stats = get_stats() or {}
        except Exception:  # noqa: BLE001
            stats = {}

    elapsed = round(time.time() - started, 1)
    llm_calls = budget.get("llm_calls", 0)
    performance = {
        "elapsed_seconds": elapsed,
        "llm_call_count": llm_calls,
        "llm_avg_latency_seconds": stats.get("llm_avg_latency_seconds", 0.0),
        "llm_prompt_tokens": stats.get("llm_prompt_tokens", 0),
        "llm_completion_tokens": stats.get("llm_completion_tokens", 0),
        "llm_total_tokens": stats.get("llm_total_tokens", 0),
        "qps": round(llm_calls / elapsed, 3) if elapsed else 0.0,
        "tps": round(stats.get("llm_total_tokens", 0) / elapsed, 1) if elapsed else 0.0,
        # --- 신규 ---
        "probe_runs": budget.get("probe_runs", 0),
        "iterations": final.get("iteration", 0),
        "blocked": final.get("blocked", []),
    }

    return {
        "input": {
            "tabular_data": f"pandas.DataFrame<{asset_name}>",
            "asset_name": asset_name,
            "source_description": source_description,
            "data_sample": _render_sample(profile),
            "column_descriptions": column_descriptions,
        },
        "tabular_profile_output": tabular_profile_output,
        "column_context_output": column_context_output,
        "initial_asset_context_output": initial_asset_context_output,
        "asset_context": asset_context,
        "issues": issues,
        "performance": performance,
        # --- 신규: skill 선택 궤적. 강건성 분석에서 "왜 이 회차만 달랐나"를 본다 ---
        "trace": [
            {
                "iteration": h.get("iteration"),
                "phase": h.get("phase", "act"),
                "skill": h.get("skill"),
                "target": h.get("target"),
                "ok": h.get("ok"),
                "attempts": h.get("attempts"),
                "note": h.get("note"),
            }
            for h in final.get("history", [])
        ],
    }


def _empty_result(asset_name, source_description, column_descriptions, started, issues):
    return {
        "input": {
            "tabular_data": f"pandas.DataFrame<{asset_name}>",
            "asset_name": asset_name,
            "source_description": source_description,
            "data_sample": "",
            "column_descriptions": column_descriptions,
        },
        "tabular_profile_output": None,
        "column_context_output": {"column_context_summary": ""},
        "initial_asset_context_output": {"summary": None},
        "asset_context": None,
        "issues": issues,
        "performance": {"elapsed_seconds": round(time.time() - started, 1), "llm_call_count": 0},
        "trace": [],
    }


def _render_profile_summary(profile: Dict[str, Any]) -> str:
    lines = []
    for c in profile.get("columns", []):
        parts = [f"**{c['name']}**: Data is of type {c.get('dtype','')}."]
        n = c.get("distinct_count", 0)
        parts.append("There is 1 unique value." if n == 1 else f"There are {n} unique values.")
        if c.get("min_value") or c.get("max_value"):
            parts.append(f"Coverage spans from {c.get('min_value')} to {c.get('max_value')}.")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _render_coverage_summary(profile: Dict[str, Any], semantics: Dict[str, Any]) -> str:
    lines = []
    for c in profile.get("columns", []):
        if c.get("kind") != "temporal":
            continue
        res = (semantics.get(c["name"], {}) or {}).get("resolution", "")
        lines.append(
            f"The temporal coverage is defined by {c['name']}"
            + (f" at {res} resolution" if res else "")
            + f" and spans from {c.get('min_value')} to {c.get('max_value')}."
        )
    return "\n".join(lines)


def _render_sample(profile: Dict[str, Any]) -> str:
    cols = profile.get("columns", [])
    if not cols:
        return ""
    header = ",".join(c["name"] for c in cols)
    depth = min((len(c.get("samples", [])) for c in cols), default=0)
    rows = [",".join(str(c["samples"][i]) for c in cols) for i in range(depth)]
    return "\n".join([header] + rows)


def _run_sync(coro):
    """동기 호출자(run_robustness_test.py)에서 async 그래프를 돌린다."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 이미 이벤트 루프 안이면 별도 스레드에서 새 루프를 돌린다.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
