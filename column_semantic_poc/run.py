#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CSV Schema Semantics PoC
========================
Folder structure:
    run.py
    skills/
        planner.md
        semantic_type.md
        column_interpretation.md
        relation_analysis.md
        semantic_validation.md
        table_context.md

Install:
    pip install -U pandas numpy openai

Required environment variables (k8s 안에서는 secret `sh-ard-asset-agent-secret`이
envFrom으로 이 값들을 주입한다 — 로컬 실행 시에만 .env에 채운다):
    LLM_API_ENDPOINT=http://<vllm-host>:8000/v1
    LLM_API_KEY=EMPTY
    LLM_MODEL=<served-model-name>

Run:
    python run.py ./data.csv

Optional:
    python run.py ./data.csv --output result.json --max-analysis-rows 50000 --max-rounds 2
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # k8s에서는 secret이 envFrom으로 환경변수를 직접 주입하므로 .env 파일이
    # 없다 - python-dotenv는 로컬 개발 편의용일 뿐이라 없어도 계속 진행한다.
    pass


SKILL_ORDER = [
    "semantic_type",
    "column_interpretation",
    "relation_analysis",
    "semantic_validation",
    "table_context",
]
REQUIRED_FIRST_PASS = {"semantic_type", "column_interpretation", "semantic_validation", "table_context"}


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------

def json_safe(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        if math.isnan(float(v)) or math.isinf(float(v)):
            return None
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if pd.isna(v):
        return None
    return v


def clean_for_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_for_json(v) for v in obj]
    return json_safe(obj)


def split_tokens(name: str) -> List[str]:
    # snake/kebab/space + camelCase + letter/number boundaries
    x = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    x = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", x)
    x = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", x)
    return [t.lower() for t in re.split(r"[_\-\s./]+", x) if t]


def read_csv_safely(path: Path) -> pd.DataFrame:
    errors = []
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=enc, low_memory=False)
        except Exception as e:
            errors.append(f"{enc}: {e}")
    raise RuntimeError("CSV를 읽지 못했습니다.\n" + "\n".join(errors))


def sample_values(s: pd.Series, n: int = 12) -> List[Any]:
    vals = s.dropna().drop_duplicates()
    if len(vals) > n:
        vals = vals.sample(n=n, random_state=42)
    return [json_safe(v) for v in vals.tolist()]


def safe_quantile(s: pd.Series, q: float) -> Optional[float]:
    try:
        v = pd.to_numeric(s, errors="coerce").quantile(q)
        return None if pd.isna(v) else float(v)
    except Exception:
        return None


def numeric_profile(s: pd.Series) -> Optional[Dict[str, Any]]:
    x = pd.to_numeric(s, errors="coerce")
    valid = x.notna().mean()
    if valid < 0.95:
        return None
    x = x.dropna()
    if len(x) == 0:
        return None
    integer_like = bool(np.allclose(x.to_numpy(), np.round(x.to_numpy()), equal_nan=True))
    return {
        "parse_ratio": float(valid),
        "min": float(x.min()),
        "max": float(x.max()),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "q1": safe_quantile(x, 0.25),
        "q3": safe_quantile(x, 0.75),
        "std": float(x.std()) if len(x) > 1 else 0.0,
        "integer_like": integer_like,
        "non_negative_ratio": float((x >= 0).mean()),
        "zero_ratio": float((x == 0).mean()),
    }


def datetime_profile(s: pd.Series) -> Optional[Dict[str, Any]]:
    # Avoid treating plain numeric series as datetimes.
    if pd.api.types.is_numeric_dtype(s):
        return None
    raw = s.dropna()
    if len(raw) == 0:
        return None
    text = raw.astype(str)
    # Fast plausibility guard: dates/times usually contain separators or date-like lengths.
    plausible = text.str.contains(r"[-/:T ]", regex=True).mean()
    if plausible < 0.5:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    ratio = parsed.notna().mean()
    if ratio < 0.8:
        return None
    parsed = parsed.dropna()
    return {
        "parse_ratio": float(ratio),
        "min": parsed.min().isoformat() if len(parsed) else None,
        "max": parsed.max().isoformat() if len(parsed) else None,
        "monotonic_increasing": bool(parsed.is_monotonic_increasing),
    }


def text_profile(s: pd.Series) -> Dict[str, Any]:
    x = s.dropna().astype(str)
    if len(x) == 0:
        return {}
    lengths = x.str.len()
    return {
        "avg_length": float(lengths.mean()),
        "min_length": int(lengths.min()),
        "max_length": int(lengths.max()),
        "digit_only_ratio": float(x.str.fullmatch(r"\d+").fillna(False).mean()),
        "alpha_only_ratio": float(x.str.fullmatch(r"[A-Za-z가-힣]+").fillna(False).mean()),
    }


def infer_physical_type(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "bool"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_float_dtype(s):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    return "string_or_mixed"


# ---------------------------------------------------------------------
# Deterministic evidence extraction
# ---------------------------------------------------------------------

def profile_columns(df: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    n = max(len(df), 1)

    for col in df.columns:
        s = df[col]
        non_null = s.notna().sum()
        nunique = s.nunique(dropna=True)
        physical = infer_physical_type(s)
        num_prof = numeric_profile(s)
        dt_prof = datetime_profile(s)

        freq = s.value_counts(dropna=False, normalize=True).head(8)
        top_values = [
            {"value": json_safe(idx), "ratio": float(ratio)}
            for idx, ratio in freq.items()
        ]

        profile = {
            "name": str(col),
            "tokens": split_tokens(str(col)),
            "physical_type": physical,
            "row_count": int(len(s)),
            "non_null_count": int(non_null),
            "null_ratio": float(1 - non_null / n),
            "nunique": int(nunique),
            "unique_ratio_non_null": float(nunique / non_null) if non_null else 0.0,
            "sample_values": sample_values(s),
            "top_values": top_values,
            "numeric_profile": num_prof,
            "datetime_profile": dt_prof,
            "text_profile": text_profile(s) if physical == "string_or_mixed" else None,
        }

        if num_prof and num_prof["integer_like"] and nunique <= 20:
            profile["small_integer_domain"] = sorted(
                [json_safe(v) for v in pd.to_numeric(s, errors="coerce").dropna().drop_duplicates().tolist()]
            )[:20]

        out[str(col)] = profile

    return out


def relation_evidence(
    df: pd.DataFrame,
    column_profiles: Dict[str, Any],
    max_pairs: int = 300,
) -> Dict[str, Any]:
    cols = [str(c) for c in df.columns]
    relations: List[Dict[str, Any]] = []

    pairs = list(itertools.combinations(cols, 2))
    if len(pairs) > max_pairs:
        # Prefer columns that are not almost entirely unique/free text.
        score = {
            c: (
                1 if column_profiles[c]["datetime_profile"] else 0,
                -column_profiles[c]["unique_ratio_non_null"],
            )
            for c in cols
        }
        ranked = sorted(cols, key=lambda c: score[c], reverse=True)
        pairs = list(itertools.combinations(ranked[: min(len(ranked), 25)], 2))[:max_pairs]

    for a, b in pairs:
        sa, sb = df[a], df[b]
        valid = sa.notna() & sb.notna()
        if valid.sum() < 3:
            continue

        av, bv = sa[valid], sb[valid]
        evidence: Dict[str, Any] = {"columns": [a, b]}

        # Exact equality
        try:
            eq_ratio = float((av.astype(str) == bv.astype(str)).mean())
            if eq_ratio >= 0.8:
                evidence["exact_equal_ratio"] = eq_ratio
        except Exception:
            pass

        # Numeric relationships
        na = pd.to_numeric(av, errors="coerce")
        nb = pd.to_numeric(bv, errors="coerce")
        num_valid = na.notna() & nb.notna()
        if num_valid.mean() >= 0.95 and num_valid.sum() >= 5:
            xa, xb = na[num_valid], nb[num_valid]
            if xa.nunique() > 1 and xb.nunique() > 1:
                corr = xa.corr(xb)
                if pd.notna(corr) and abs(corr) >= 0.5:
                    evidence["pearson_corr"] = float(corr)
            evidence["a_le_b_ratio"] = float((xa <= xb).mean())
            diff = xb - xa
            evidence["b_minus_a"] = {
                "median": float(diff.median()),
                "min": float(diff.min()),
                "max": float(diff.max()),
            }

        # Temporal ordering
        pa = column_profiles[a].get("datetime_profile")
        pb = column_profiles[b].get("datetime_profile")
        if pa and pb:
            da = pd.to_datetime(av.astype(str), errors="coerce")
            db = pd.to_datetime(bv.astype(str), errors="coerce")
            if getattr(da.dt, "tz", None) is not None:
                da = da.dt.tz_convert("UTC").dt.tz_localize(None)
            if getattr(db.dt, "tz", None) is not None:
                db = db.dt.tz_convert("UTC").dt.tz_localize(None)
            dv = da.notna() & db.notna()
            if dv.sum() >= 3:
                da2, db2 = da[dv], db[dv]
                delta = (db2 - da2).dt.total_seconds()
                evidence["temporal"] = {
                    "a_le_b_ratio": float((da2 <= db2).mean()),
                    "median_delta_seconds": float(delta.median()),
                    "negative_delta_ratio": float((delta < 0).mean()),
                }

        # Mapping consistency for hierarchy / functional dependency.
        # b -> a: for each b, does it map to one a?
        if av.nunique() <= 5000 and bv.nunique() <= 5000:
            try:
                b_to_a = pd.DataFrame({"a": av.astype(str), "b": bv.astype(str)}).drop_duplicates()
                cnt = b_to_a.groupby("b")["a"].nunique()
                if len(cnt):
                    weighted_ok = float(
                        bv.astype(str).isin(cnt[cnt == 1].index).mean()
                    )
                    if weighted_ok >= 0.9:
                        evidence["b_to_a_mapping_consistency"] = weighted_ok

                a_to_b = b_to_a.groupby("a")["b"].nunique()
                if len(a_to_b):
                    weighted_ok = float(
                        av.astype(str).isin(a_to_b[a_to_b == 1].index).mean()
                    )
                    if weighted_ok >= 0.9:
                        evidence["a_to_b_mapping_consistency"] = weighted_ok
            except Exception:
                pass

        if len(evidence) > 1:
            relations.append(evidence)

    return {"pairwise": relations}


def find_grain_candidates(df: pd.DataFrame, max_width: int = 3, max_cols: int = 20) -> List[Dict[str, Any]]:
    cols = [str(c) for c in df.columns[:max_cols]]
    n = len(df)
    if n == 0:
        return []

    candidates = []

    def uniqueness(combo: Tuple[str, ...]) -> float:
        return float(df[list(combo)].drop_duplicates().shape[0] / n)

    # Singles first.
    for c in cols:
        u = uniqueness((c,))
        if u >= 0.98:
            candidates.append({"columns": [c], "unique_ratio": u})

    # Search composite keys, but stop widening once exact/near-exact candidates exist.
    for width in range(2, max_width + 1):
        width_results = []
        for combo in itertools.combinations(cols, width):
            u = uniqueness(combo)
            if u >= 0.98:
                width_results.append({"columns": list(combo), "unique_ratio": u})
                if len(width_results) >= 12:
                    break
        candidates.extend(width_results)
        if width_results:
            break

    return sorted(candidates, key=lambda x: (len(x["columns"]), -x["unique_ratio"]))[:15]


def build_table_evidence(df: pd.DataFrame) -> Dict[str, Any]:
    profiles = profile_columns(df)
    rel = relation_evidence(df, profiles)
    grain = find_grain_candidates(df)

    return clean_for_json({
        "table": {
            "row_count": int(len(df)),
            "column_count": int(len(df.columns)),
            "columns": [str(c) for c in df.columns],
        },
        "column_profiles": profiles,
        "relation_evidence": rel,
        "grain_candidates": grain,
    })


# ---------------------------------------------------------------------
# Skill-requested data probes
#
# Skills cannot see row-level data, only aggregated evidence, so they cannot
# reliably compute an exact cross-column relationship themselves. When a
# skill's output attaches a `probe` request to a check — an arithmetic
# expression over named columns, e.g. "a + b" or "a <= b" — this evaluates
# that specific, skill-declared expression against the real dataframe and
# returns the measured result. It is a general-purpose safe calculator, not
# a fixed list of named operations: it does not decide *which* claims are
# worth checking or *what* relationship to test (that is the skill's job),
# it only knows how to safely evaluate arithmetic/comparison expressions.
# Extending what a skill can ask for means extending the expression it
# writes, not adding a case here.
# ---------------------------------------------------------------------

import ast
import operator

_PROBE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_PROBE_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_PROBE_COMPARE = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}
_PROBE_FUNCS = {"abs": np.abs, "min": np.minimum, "max": np.maximum, "round": np.round}


class ProbeExpressionError(Exception):
    pass


def _eval_probe_node(node: ast.AST, variables: Dict[str, pd.Series]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_probe_node(node.body, variables)
    if isinstance(node, ast.BinOp) and type(node.op) in _PROBE_BINOPS:
        return _PROBE_BINOPS[type(node.op)](
            _eval_probe_node(node.left, variables), _eval_probe_node(node.right, variables)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _PROBE_UNARYOPS:
        return _PROBE_UNARYOPS[type(node.op)](_eval_probe_node(node.operand, variables))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _PROBE_COMPARE:
        return _PROBE_COMPARE[type(node.ops[0])](
            _eval_probe_node(node.left, variables), _eval_probe_node(node.comparators[0], variables)
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _PROBE_FUNCS
        and not node.keywords
    ):
        return _PROBE_FUNCS[node.func.id](*(_eval_probe_node(a, variables) for a in node.args))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ProbeExpressionError(f"unknown variable: {node.id}")
        return variables[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise ProbeExpressionError(f"disallowed expression: {ast.dump(node)}")


def eval_probe_expression(expression: str, variables: Dict[str, pd.Series]) -> Any:
    tree = ast.parse(expression, mode="eval")
    return _eval_probe_node(tree, variables)


def run_probe(df: pd.DataFrame, probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    expression = probe.get("expression")
    columns = probe.get("columns")
    if not isinstance(expression, str) or not isinstance(columns, dict) or not columns:
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in columns.items()):
        return None
    if not all(col in df.columns for col in columns.values()):
        return None

    variables: Dict[str, pd.Series] = {}
    valid = pd.Series(True, index=df.index)
    for alias, col in columns.items():
        s = pd.to_numeric(df[col], errors="coerce")
        variables[alias] = s
        valid &= s.notna()
    if valid.sum() < 3:
        return None
    variables = {k: v[valid] for k, v in variables.items()}

    try:
        result = eval_probe_expression(expression, variables)
    except (ProbeExpressionError, SyntaxError, TypeError, ZeroDivisionError):
        return None
    if not isinstance(result, pd.Series) or len(result) == 0:
        return None

    observed: Dict[str, Any] = {"expression": expression, "columns": columns, "n": int(len(result))}

    if result.dtype == bool:
        observed["true_ratio"] = float(result.mean())
    else:
        result = result.replace([np.inf, -np.inf], np.nan).dropna()
        if len(result) == 0:
            return None
        observed["median"] = float(result.median())
        observed["min"] = float(result.min())
        observed["max"] = float(result.max())
        target = probe.get("target")
        if isinstance(target, (int, float)):
            tolerance = probe.get("tolerance", 0.05)
            observed["target"] = float(target)
            observed["tolerance"] = float(tolerance)
            observed["within_tolerance_ratio"] = float(((result - target).abs() <= tolerance).mean())

    return observed


def apply_probes(df: pd.DataFrame, validation: Dict[str, Any]) -> Dict[str, Any]:
    checks = validation.get("checks")
    if not isinstance(checks, list):
        return validation

    for check in checks:
        probe = check.get("probe") if isinstance(check, dict) else None
        if not isinstance(probe, dict):
            continue
        observed = run_probe(df, probe)
        if observed is None:
            continue

        check["observed"] = observed
        check["probe_verified"] = True
        ratio = observed.get("within_tolerance_ratio", observed.get("true_ratio"))
        if ratio is not None:
            if ratio >= 0.95:
                check["status"] = "pass"
            elif ratio >= 0.7:
                check["status"] = "warning"
            else:
                check["status"] = "fail"

    if any(c.get("status") in {"warning", "fail"} for c in checks if isinstance(c, dict)):
        validation["overall_status"] = "needs_revision"
    elif checks:
        validation["overall_status"] = "pass"

    return validation


# ---------------------------------------------------------------------
# vLLM (OpenAI-compatible)
# ---------------------------------------------------------------------

def make_client() -> Tuple[Any, str]:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai 패키지를 사용할 수 없습니다. `pip install -U openai`로 설치하세요."
        ) from e
    endpoint = os.getenv("LLM_API_ENDPOINT")
    api_key = os.getenv("LLM_API_KEY", "EMPTY")
    model = os.getenv("LLM_MODEL")

    missing = [
        name for name, value in [
            ("LLM_API_ENDPOINT", endpoint),
            ("LLM_MODEL", model),
        ] if not value
    ]
    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: " + ", ".join(missing)
        )

    client = OpenAI(
        base_url=endpoint,
        api_key=api_key,
    )
    return client, model


def parse_json_text(text: str) -> Dict[str, Any]:
    text = text.strip()

    # Remove markdown fences if the model ignored "JSON only".
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: extract the outermost object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError(f"JSON 응답 파싱 실패:\n{text[:1500]}")


def llm_json(
    client: Any,
    model: str,
    system_prompt: str,
    payload: Dict[str, Any],
    max_retries: int = 1,
) -> Dict[str, Any]:
    user_text = json.dumps(clean_for_json(payload), ensure_ascii=False, indent=2)

    last_error = None
    for attempt in range(max_retries + 1):
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "아래 입력을 처리하고 반드시 JSON object 하나만 반환하세요.\n\n"
                    + user_text
                ),
            },
        ]
        if attempt > 0:
            messages.append({
                "role": "user",
                "content": "이전 응답은 JSON 파싱에 실패했습니다. 설명/마크다운 없이 유효한 JSON만 반환하세요."
            })

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
            )
            text = resp.choices[0].message.content or ""
            return parse_json_text(text)
        except Exception as e:
            last_error = e

    raise RuntimeError(f"LLM 호출 실패: {last_error}")


# ---------------------------------------------------------------------
# Skills + Plan/Execute
# ---------------------------------------------------------------------

class SkillRunner:
    def __init__(self, skill_dir: Path, client: Any, model: str):
        self.skill_dir = skill_dir
        self.client = client
        self.model = model
        self.skills = self._load_skills()

    def _load_skills(self) -> Dict[str, str]:
        found = {}
        for path in self.skill_dir.glob("*.md"):
            found[path.stem] = path.read_text(encoding="utf-8")
        required = {"planner", *SKILL_ORDER}
        missing = required - set(found)
        if missing:
            raise RuntimeError(f"skills 폴더에 필요한 skill이 없습니다: {sorted(missing)}")
        return found

    def plan(
        self,
        evidence: Dict[str, Any],
        previous_results: Optional[Dict[str, Any]] = None,
        validation_feedback: Optional[Dict[str, Any]] = None,
        replan: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "objective": (
                "CSV의 컬럼 의미를 evidence 기반으로 추론하고, "
                "오류 전파를 최소화하면서 최종 Table Context를 생성한다."
            ),
            "mode": "replan" if replan else "first_pass",
            "available_skills": SKILL_ORDER,
            "table_summary": evidence["table"],
            "grain_candidates": evidence.get("grain_candidates", []),
            "has_pairwise_evidence": bool(
                evidence.get("relation_evidence", {}).get("pairwise")
            ),
            "previous_results": previous_results if replan else None,
            "validation_feedback": validation_feedback,
        }
        plan = llm_json(
            self.client,
            self.model,
            self.skills["planner"],
            payload,
        )
        return self._sanitize_plan(plan, replan=replan)

    def _sanitize_plan(self, plan: Dict[str, Any], replan: bool) -> Dict[str, Any]:
        raw_steps = plan.get("steps", [])
        valid_steps = []
        seen = set()

        for step in raw_steps:
            skill = step.get("skill")
            if skill in SKILL_ORDER and skill not in seen:
                valid_steps.append({
                    "skill": skill,
                    "goal": step.get("goal", ""),
                    "focus": step.get("focus", []),
                })
                seen.add(skill)

        if not replan:
            # Guarantee the safety-critical skeleton even if the LLM planner omits it.
            for required in ["semantic_type", "column_interpretation"]:
                if required not in seen:
                    valid_steps.append({"skill": required, "goal": "required first-pass step", "focus": []})
                    seen.add(required)

            if "semantic_validation" not in seen:
                valid_steps.append({"skill": "semantic_validation", "goal": "required validation", "focus": []})
                seen.add("semantic_validation")
            if "table_context" not in seen:
                valid_steps.append({"skill": "table_context", "goal": "required final synthesis", "focus": []})
                seen.add("table_context")

        # Enforce canonical order.
        valid_steps.sort(key=lambda x: SKILL_ORDER.index(x["skill"]))
        plan["steps"] = valid_steps
        return plan

    def execute_skill(
        self,
        skill_name: str,
        evidence: Dict[str, Any],
        results: Dict[str, Any],
        focus: Optional[List[str]] = None,
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:

        # First semantic passes get raw evidence + only the minimum allowed prior outputs.
        if skill_name == "semantic_type":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "raw_other_column_names": evidence["table"]["columns"],
                "focus": focus or [],
            }

        elif skill_name == "column_interpretation":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "raw_other_column_names": evidence["table"]["columns"],
                "semantic_type": results.get("semantic_type"),
                "revision_feedback": revision_feedback,
                "focus": focus or [],
            }

        elif skill_name == "relation_analysis":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "relation_evidence": evidence["relation_evidence"],
                "grain_candidates": evidence["grain_candidates"],
                "semantic_type": results.get("semantic_type"),
                "column_interpretation": results.get("column_interpretation"),
                "previous_relation_analysis": results.get("relation_analysis"),
                "revision_feedback": revision_feedback,
                "focus": focus or [],
            }

        elif skill_name == "semantic_validation":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "relation_evidence": evidence["relation_evidence"],
                "grain_candidates": evidence["grain_candidates"],
                "semantic_type": results.get("semantic_type"),
                "column_interpretation": results.get("column_interpretation"),
                "relation_analysis": results.get("relation_analysis"),
                "revision_feedback": revision_feedback,
                "focus": focus or [],
            }

        elif skill_name == "table_context":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "grain_candidates": evidence["grain_candidates"],
                "semantic_type": results.get("semantic_type"),
                "column_interpretation": results.get("column_interpretation"),
                "relation_analysis": results.get("relation_analysis"),
                "semantic_validation": results.get("semantic_validation"),
                "focus": focus or [],
            }

        else:
            raise ValueError(f"알 수 없는 skill: {skill_name}")

        return llm_json(
            self.client,
            self.model,
            self.skills[skill_name],
            payload,
        )


def run_pipeline(
    csv_path: Path,
    skill_dir: Path,
    max_analysis_rows: int = 50000,
    max_rounds: int = 2,
) -> Dict[str, Any]:
    raw_df = read_csv_safely(csv_path)

    if len(raw_df) > max_analysis_rows:
        df = raw_df.sample(max_analysis_rows, random_state=42).reset_index(drop=True)
        sampled = True
    else:
        df = raw_df.copy()
        sampled = False

    print(f"[LOAD] {csv_path.name}: {len(raw_df):,} rows x {len(raw_df.columns)} cols")
    if sampled:
        print(f"[PROFILE] 분석 비용 제한으로 {len(df):,}개 행 샘플 사용")

    evidence = build_table_evidence(df)
    evidence["table"]["source_file"] = csv_path.name
    evidence["table"]["original_row_count"] = int(len(raw_df))
    evidence["table"]["analysis_sampled"] = sampled
    evidence["table"]["analysis_row_count"] = int(len(df))

    client, model = make_client()
    runner = SkillRunner(skill_dir, client, model)

    results: Dict[str, Any] = {}
    plans: List[Dict[str, Any]] = []

    # First pass -------------------------------------------------------
    plan = runner.plan(evidence)
    plans.append(plan)
    print("[PLAN 1]", " -> ".join(step["skill"] for step in plan["steps"]))

    for step in plan["steps"]:
        skill = step["skill"]
        print(f"[EXEC] {skill}")
        results[skill] = runner.execute_skill(
            skill,
            evidence,
            results,
            focus=step.get("focus", []),
        )
        if skill == "semantic_validation":
            results[skill] = apply_probes(df, results[skill])

    # Revision passes -------------------------------------------------
    for round_idx in range(2, max_rounds + 1):
        validation = results.get("semantic_validation") or {}
        if validation.get("overall_status") != "needs_revision":
            break

        feedback = {
            "revision_requests": validation.get("revision_requests", []),
            "checks": [
                x for x in validation.get("checks", [])
                if x.get("status") in {"warning", "fail"}
            ],
        }
        replan = runner.plan(
            evidence,
            previous_results=results,
            validation_feedback=feedback,
            replan=True,
        )

        # A revision must end in validation; table context is refreshed after validation.
        skills_to_run = [s for s in replan.get("steps", []) if s["skill"] != "table_context"]
        if "semantic_validation" not in [s["skill"] for s in skills_to_run]:
            skills_to_run.append({
                "skill": "semantic_validation",
                "goal": "validate revised interpretation",
                "focus": [],
            })
        skills_to_run.sort(key=lambda x: SKILL_ORDER.index(x["skill"]))

        plans.append({"reason": replan.get("reason", ""), "steps": skills_to_run})
        print(f"[PLAN {round_idx}]", " -> ".join(step["skill"] for step in skills_to_run))

        for step in skills_to_run:
            skill = step["skill"]
            print(f"[RE-EXEC] {skill}")
            results[skill] = runner.execute_skill(
                skill,
                evidence,
                results,
                focus=step.get("focus", []),
                revision_feedback=feedback,
            )
            if skill == "semantic_validation":
                results[skill] = apply_probes(df, results[skill])

        # Always regenerate final table context after a revision.
        print("[RE-EXEC] table_context")
        results["table_context"] = runner.execute_skill(
            "table_context",
            evidence,
            results,
            revision_feedback=feedback,
        )

    return clean_for_json({
        "meta": {
            "source_csv": str(csv_path),
            "skills_dir": str(skill_dir),
            "llm_model": model,
            "max_rounds": max_rounds,
        },
        "plans": plans,
        "evidence": evidence,
        "results": results,
    })


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CSV를 Plan-Execute + Skills 구조로 분석해 컬럼/테이블 의미를 추론합니다."
    )
    parser.add_argument("csv", type=Path, help="입력 CSV 경로")
    parser.add_argument(
        "--skills",
        type=Path,
        default=Path(__file__).resolve().parent / "skills",
        help="skills 폴더 경로",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="출력 JSON 경로. 기본값: <csv>.semantic.json",
    )
    parser.add_argument(
        "--max-analysis-rows",
        type=int,
        default=50000,
        help="프로파일링에 사용할 최대 행 수",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=2,
        help="검증 실패 시 최대 Plan/Execute 라운드",
    )
    args = parser.parse_args()

    csv_path = args.csv.resolve()
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    output = args.output
    if output is None:
        output = csv_path.with_suffix(csv_path.suffix + ".semantic.json")

    result = run_pipeline(
        csv_path=csv_path,
        skill_dir=args.skills.resolve(),
        max_analysis_rows=args.max_analysis_rows,
        max_rounds=args.max_rounds,
    )

    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[DONE] {output}")


if __name__ == "__main__":
    main()
