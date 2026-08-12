from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List

from .contract import SkillDeps, Slot
from .graph import build_agent
from .state import load_board, new_state

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class TableAssetContextRunner:
    """현재 graph/state 구조를 직접 실행하는 얇은 runner."""

    def __init__(
        self,
        deps: SkillDeps,
        skills_dir: Path | None = None,
        max_iterations: int = 24,
        max_llm_calls: int = 300,
    ) -> None:
        self.deps = deps
        self.skills_dir = skills_dir or SKILLS_DIR
        self.max_iterations = max_iterations
        self.max_llm_calls = max_llm_calls
        self._agent = build_agent(self.skills_dir, self.deps)

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
            return _empty_result(
                asset_name=asset_name,
                source_description=source_description,
                column_descriptions=column_descriptions,
                started=started,
                issues=[{"stage": "agent", "error_type": type(exc).__name__, "message": str(exc)}],
            )

        return _to_result(
            final=final,
            asset_name=asset_name,
            source_description=source_description,
            column_descriptions=column_descriptions,
            started=started,
            deps=self.deps,
        )


def _to_result(
    final: Dict[str, Any],
    asset_name: str,
    source_description: str | None,
    column_descriptions: Dict[str, str] | None,
    started: float,
    deps: SkillDeps,
) -> Dict[str, Any]:
    board = load_board(final)
    values = board.values
    keyed = board.keyed

    profile = values.get(Slot.TABLE_PROFILE.value) or {}
    semantics = keyed.get(Slot.COLUMN_SEMANTICS.value, {})
    distribution_profiles = keyed.get(Slot.DISTRIBUTION_PROFILE.value, {})
    value_patterns = keyed.get(Slot.VALUE_PATTERNS.value, {})
    join_candidates = keyed.get(Slot.JOIN_CANDIDATES.value, {})
    grain = values.get(Slot.GRAIN.value) or {}
    linkage = values.get(Slot.LINKAGE.value) or []
    constraints = values.get(Slot.CONSTRAINTS.value) or {}
    quality_risks = values.get(Slot.QUALITY_RISKS.value) or {}
    compliance = values.get(Slot.COMPLIANCE.value) or {}
    glossary = values.get(Slot.GLOSSARY.value) or {}
    analysis_plan = values.get(Slot.ANALYSIS_PLAN.value) or {}
    ctx = values.get(Slot.ASSET_CONTEXT.value) or {}
    verification = values.get(Slot.VERIFICATION.value) or {}

    columns = _build_columns(
        profile,
        semantics,
        distribution_profiles,
        value_patterns,
        join_candidates,
    )
    issues = _collect_issues(final, verification)
    performance = _build_performance(final, deps, started)

    asset_context = {
        "topic": ctx.get("topic", ""),
        "summary": ctx.get("summary", values.get(Slot.SUMMARY.value, "")),
        "search_terms": ctx.get("search_terms", values.get(Slot.SEARCH_TERMS.value, [])),
        "asset_context_details": {
            "summary": ctx.get("summary", ""),
            "key_points": ctx.get("key_points", []),
            "use_cases": ctx.get("use_cases", []),
            "related_concepts": ctx.get("related_concepts", []),
            "keywords": ctx.get("keywords", ctx.get("search_terms", [])),
            "additional_context": ctx.get("additional_context", []),
        },
        "grain": grain.get("grain", ""),
        "grain_keys": grain.get("key_columns", []),
        "record_unit": grain.get("grain", ""),
        "linkage": linkage,
        "constraints": constraints,
        "quality_risks": quality_risks,
        "compliance": compliance,
        "glossary": glossary,
        "verification": _normalize_verification(verification),
        "analysis_plan": analysis_plan,
        "columns": columns,
    }

    data_interpretation = {
        "grain": asset_context["grain"],
        "grain_keys": asset_context["grain_keys"],
        "record_unit": asset_context["record_unit"],
        "time_axes": [
            {"name": c["name"], "resolution": c.get("resolution", ""), "time_unit": c.get("resolution", "")}
            for c in columns
            if c.get("profiled_kind") == "temporal"
        ],
        "linkage": linkage,
        "constraints": constraints,
        "quality_risks": quality_risks,
        "compliance": compliance,
        "glossary": glossary,
        "verification": asset_context["verification"],
        "analysis_plan": analysis_plan,
    }

    return {
        "input": {
            "tabular_data": f"pandas.DataFrame<{asset_name}>",
            "asset_name": asset_name,
            "source_description": source_description,
            "column_descriptions": column_descriptions,
        },
        "column_analysis": {
            "columns": columns,
            "summary": _column_summary(columns),
        },
        "data_interpretation": data_interpretation,
        "asset_context": asset_context,
        "issues": issues,
        "performance": performance,
        "trace": [
            {
                "iteration": h.get("iteration"),
                "phase": h.get("phase", "act"),
                "skill": h.get("skill"),
                "target": h.get("target"),
                "ok": h.get("ok"),
                "attempts": h.get("attempts"),
                "artifacts": h.get("artifacts"),
                "analysis_needs": h.get("analysis_needs"),
                "note": h.get("note"),
                "reason": h.get("reason"),
            }
            for h in final.get("history", [])
        ],
        "analysis_artifacts": board.artifacts,
    }


def _build_columns(
    profile: Dict[str, Any],
    semantics: Dict[str, Any],
    distribution_profiles: Dict[str, Any],
    value_patterns: Dict[str, Any],
    join_candidates: Dict[str, Any],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for col in profile.get("columns", []):
        name = col["name"]
        semantic = semantics.get(name, {})
        merged = {
            "name": name,
            "profiled_kind": col.get("kind", ""),
            "dtype": col.get("dtype", ""),
            "distinct_count": col.get("distinct_count", 0),
            "distinct_ratio": col.get("distinct_ratio", 0.0),
            "null_ratio": col.get("null_ratio", 0.0),
            "avg_char_length": col.get("avg_char_length", 0.0),
            "min_value": col.get("min_value", ""),
            "max_value": col.get("max_value", ""),
            "samples": col.get("samples", []),
        }
        merged.update(semantic)
        if name in distribution_profiles:
            merged["distribution_profile"] = distribution_profiles[name]
        if name in value_patterns:
            merged["value_pattern"] = value_patterns[name]
        if name in join_candidates:
            merged["join_candidate"] = join_candidates[name]
        out.append(merged)
    return out


def _normalize_verification(verification: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": verification.get("status"),
        "verified": len(verification.get("verified", [])),
        "refuted": [e.get("statement") for e in verification.get("refuted", [])],
        "unverified_count": len(verification.get("unverified", [])),
        "probe_coverage": verification.get("coverage", 0.0),
        "contradictions": verification.get("contradictions", []),
    }


def _collect_issues(final: Dict[str, Any], verification: Dict[str, Any]) -> List[Dict[str, Any]]:
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
            {
                "stage": "verification",
                "error_type": None,
                "message": f"{entry.get('statement')} — {entry.get('detail')}"[:500],
            }
        )
    return issues


def _build_performance(final: Dict[str, Any], deps: SkillDeps, started: float) -> Dict[str, Any]:
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
    return {
        "elapsed_seconds": elapsed,
        "llm_call_count": llm_calls,
        "llm_avg_latency_seconds": stats.get("llm_avg_latency_seconds", 0.0),
        "llm_prompt_tokens": stats.get("llm_prompt_tokens", 0),
        "llm_completion_tokens": stats.get("llm_completion_tokens", 0),
        "llm_total_tokens": stats.get("llm_total_tokens", 0),
        "qps": round(llm_calls / elapsed, 3) if elapsed else 0.0,
        "tps": round(stats.get("llm_total_tokens", 0) / elapsed, 1) if elapsed else 0.0,
        "probe_runs": budget.get("probe_runs", 0),
        "iterations": final.get("iteration", 0),
        "blocked": final.get("blocked", []),
    }


def _column_summary(columns: List[Dict[str, Any]]) -> str:
    if not columns:
        return ""
    bits = []
    for c in columns[:8]:
        bits.append(f"{c['name']}({c.get('profiled_kind','unknown')})")
    return ", ".join(bits)


def _empty_result(asset_name, source_description, column_descriptions, started, issues):
    return {
        "input": {
            "tabular_data": f"pandas.DataFrame<{asset_name}>",
            "asset_name": asset_name,
            "source_description": source_description,
            "column_descriptions": column_descriptions,
        },
        "column_analysis": {"columns": [], "summary": ""},
        "data_interpretation": {},
        "asset_context": None,
        "issues": issues,
        "performance": {"elapsed_seconds": round(time.time() - started, 1), "llm_call_count": 0},
        "trace": [],
    }


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
