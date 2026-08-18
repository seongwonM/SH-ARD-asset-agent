#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CSV Schema Semantics PoC
========================
Folder structure:
    run.py
    csv_repair.py
    skills/
        planner.md               1차 결과 검증 실패 시 재계획(replan) 전용
        gap_planner.md            1차 해석 직후, 컬럼별 보충 skill 배정 판단
        semantic_type.md
        column_interpretation.md  컬럼별 병렬 호출 (1 call = 1 column)
        relation_analysis.md
        semantic_validation.md    관계그룹별 병렬 호출 (1 call = 1 group/단일컬럼배치)
        table_context.md
        reconsider_ambiguous.md   gap skill: ambiguous 컬럼을 전체 문맥으로 재조정
        explain_sparsity.md       gap skill: null/zero 비율이 높은 이유 추론
        reconcile_type_meaning.md gap skill: semantic_type-meaning 충돌 해소

1차 pass 순서(semantic_type -> column_interpretation -> [relation_analysis])는
고정이라 LLM 계획 호출이 없다. planner.md는 검증 실패 후 재계획에만 쓰인다.

Install:
    pip install -U pandas numpy openai

Required environment variables (k8s 안에서는 secret `sh-ard-asset-agent-secret`이
envFrom으로 이 값들을 주입한다 — 로컬 실행 시에만 .env에 채운다, python-dotenv 불필요, 자체 파서):
    LLM_API_ENDPOINT=http://<vllm-host>:8000/v1
    LLM_API_KEY=EMPTY
    LLM_MODEL=<served-model-name>

Run:
    python run.py ./data.csv

Optional:
    python run.py ./data.csv --output result.json --max-rounds 2

Skill 하나가 끝날 때마다 <output>.partial.json에 그때까지의 plans/evidence/results를
체크포인트로 남긴다. 파이프라인이 끝까지 성공하면 <output>에 최종 결과가 쓰이고
partial 파일은 지워진다 - 중간에 실패해도 partial 파일에 그때까지 계산된
skill 출력이 남아있다.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from csv_repair import CsvRepairError, repair_ragged_csv


def load_dotenv(path: str = ".env") -> None:
    """값 뒤 인라인 주석까지 처리하는 최소 파서. 이미 설정된 환경변수는 덮지 않는다.

    k8s에서는 secret이 envFrom으로 환경변수를 직접 주입하므로 .env 파일이 없다 -
    그 경우 아무 것도 하지 않고 넘어간다. 로컬 실행 시에만 의미가 있다.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #")[0].strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


load_dotenv()


SKILL_ORDER = [
    "semantic_type",
    "column_interpretation",
    "relation_analysis",
    "semantic_validation",
    "table_context",
]

# 1차 해석 직후 컬럼별로 부족한 점이 있으면 gap_planner가 이 중에서 골라 붙인다.
# (Sato식 2단계 구조: 1차는 컬럼 독립적으로, 여기서는 다른 컬럼들의 확정 결과까지 보고 재조정)
GAP_SKILLS = ["reconsider_ambiguous", "explain_sparsity", "reconcile_type_meaning"]


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------

KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    """KST(UTC+9, 고정 오프셋) 타임스탬프. zoneinfo/tzdata에 기대지 않는다 -
    슬림 컨테이너 이미지에는 시간대 DB가 없는 경우가 많아 "Asia/Seoul" 같은
    이름 기반 시간대는 조용히 무시되거나(플랫폼에 따라 다름) 에러가 날 수 있다."""
    return datetime.now(KST).isoformat(timespec="seconds")


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
        except pd.errors.ParserError as e:
            # 값 안에 이스케이프 안 된 ","가 섞여 필드 수가 헤더보다 많아진
            # 경우일 수 있다 - 인코딩을 바꿔봐야 소용없으니 복구를 시도한다.
            try:
                return repair_ragged_csv(path, encoding=enc)
            except CsvRepairError as repair_error:
                errors.append(f"{enc}: {e} (복구 시도 실패: {repair_error})")
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


class RateLimiter:
    """RPM 상한(슬라이딩 60초 윈도) + 동시 실행 수 상한을 같이 지키는 스레드세이프 리미터.

    컬럼별/그룹별 병렬 호출이 전부 이걸 통해서 나가므로, 여러 스레드가 동시에
    acquire()해도 분당 요청 수가 requests_per_minute을 넘지 않는다.
    """

    def __init__(self, requests_per_minute: int, max_concurrency: int):
        self.max_concurrency = max(1, max_concurrency)
        self._rpm = max(1, requests_per_minute)
        self._lock = threading.Lock()
        self._timestamps: List[float] = []
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    def acquire(self) -> None:
        self._semaphore.acquire()
        while True:
            with self._lock:
                now = time.time()
                self._timestamps = [t for t in self._timestamps if now - t < 60]
                if len(self._timestamps) < self._rpm:
                    self._timestamps.append(now)
                    return
                wait = 60 - (now - self._timestamps[0])
            time.sleep(max(wait, 0.05))

    def release(self) -> None:
        self._semaphore.release()


def llm_json(
    client: Any,
    model: str,
    system_prompt: str,
    payload: Dict[str, Any],
    max_retries: int = 1,
    label: str = "",
    timeline: Optional[List[Dict[str, Any]]] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> Dict[str, Any]:
    user_text = json.dumps(clean_for_json(payload), ensure_ascii=False, indent=2)
    tag = f"[LLM:{label}]" if label else "[LLM]"

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

        started_at = now_iso()
        print(
            f"{tag} 요청 전송 (시도 {attempt + 1}/{max_retries + 1}, "
            f"model={model}, 입력 {len(user_text):,}자)",
            flush=True,
        )
        if rate_limiter is not None:
            rate_limiter.acquire()
        started = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
            )
            text = resp.choices[0].message.content or ""
            elapsed = time.time() - started
            usage = getattr(resp, "usage", None)
            total_tokens = usage.total_tokens if usage else None
            tokens = f", 토큰 {total_tokens}" if total_tokens is not None else ""
            print(f"{tag} 응답 수신 ({elapsed:.1f}초, 출력 {len(text):,}자{tokens})", flush=True)
            if timeline is not None:
                timeline.append({
                    "event": "llm_call",
                    "label": label,
                    "attempt": attempt + 1,
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "elapsed_seconds": round(elapsed, 3),
                    "status": "ok",
                    "input_chars": len(user_text),
                    "output_chars": len(text),
                    "tokens": total_tokens,
                })
            return parse_json_text(text)
        except Exception as e:
            elapsed = time.time() - started
            last_error = e
            print(f"{tag} 실패 ({elapsed:.1f}초): {type(e).__name__}: {e}", flush=True)
            if timeline is not None:
                timeline.append({
                    "event": "llm_call",
                    "label": label,
                    "attempt": attempt + 1,
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "elapsed_seconds": round(elapsed, 3),
                    "status": "error",
                    "input_chars": len(user_text),
                    "error": f"{type(e).__name__}: {e}",
                })
        finally:
            if rate_limiter is not None:
                rate_limiter.release()

    raise RuntimeError(f"LLM 호출 실패: {last_error}")


# ---------------------------------------------------------------------
# Skills + Plan/Execute
# ---------------------------------------------------------------------

class SkillRunner:
    def __init__(
        self,
        skill_dir: Path,
        client: Any,
        model: str,
        timeline: Optional[List[Dict[str, Any]]] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.skill_dir = skill_dir
        self.client = client
        self.model = model
        self.timeline = timeline
        self.rate_limiter = rate_limiter
        self.skills = self._load_skills()

    def _load_skills(self) -> Dict[str, str]:
        found = {}
        for path in self.skill_dir.glob("*.md"):
            found[path.stem] = path.read_text(encoding="utf-8")
        required = {"planner", "gap_planner", *SKILL_ORDER, *GAP_SKILLS}
        missing = required - set(found)
        if missing:
            raise RuntimeError(f"skills 폴더에 필요한 skill이 없습니다: {sorted(missing)}")
        return found

    def plan(
        self,
        evidence: Dict[str, Any],
        previous_results: Dict[str, Any],
        validation_feedback: Dict[str, Any],
    ) -> Dict[str, Any]:
        """검증 실패 후 재계획. 1차 pass는 고정 순서라 이 함수를 거치지 않는다
        (run_pipeline에서 파이썬으로 직접 순서를 정한다) - 여기는 항상 '지금까지의
        결과 + 검증 실패 이유를 보고 다음에 뭘 해야 하는지' 판단하는 replan이다."""
        payload = {
            "objective": (
                "semantic_validation이 지적한 모순을 해소하도록, 필요한 skill만 다시 실행하는 계획을 세운다."
            ),
            "available_skills": SKILL_ORDER,
            "table_summary": evidence["table"],
            "grain_candidates": evidence.get("grain_candidates", []),
            "has_pairwise_evidence": bool(
                evidence.get("relation_evidence", {}).get("pairwise")
            ),
            "previous_results": previous_results,
            "validation_feedback": validation_feedback,
        }
        plan = llm_json(
            self.client,
            self.model,
            self.skills["planner"],
            payload,
            label="replan",
            timeline=self.timeline,
            rate_limiter=self.rate_limiter,
        )
        return self._sanitize_plan(plan)

    def _sanitize_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
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

        # Enforce canonical order.
        valid_steps.sort(key=lambda x: SKILL_ORDER.index(x["skill"]))
        plan["steps"] = valid_steps
        return plan

    def diagnose_gaps(self, evidence: Dict[str, Any], results: Dict[str, Any]) -> Dict[str, Any]:
        """1차 해석(semantic_type/column_interpretation/relation_analysis) 직후 호출.
        컬럼별 상태(ambiguous 여부, null/zero 비율, 타입-의미 충돌)를 보고 어떤 컬럼에
        GAP_SKILLS 중 무엇을 붙일지 LLM이 판단한다 - 규칙표가 아니라 판단이 필요한
        지점이라 여기만 planner에게 맡긴다."""
        column_interp = (results.get("column_interpretation") or {}).get("columns", {})
        semantic_type = (results.get("semantic_type") or {}).get("columns", {})
        columns_summary = []
        for col in evidence["table"]["columns"]:
            profile = evidence["column_profiles"].get(col, {})
            numeric = profile.get("numeric_profile") or {}
            columns_summary.append({
                "name": col,
                "semantic_type": semantic_type.get(col),
                "interpretation": column_interp.get(col),
                "null_ratio": profile.get("null_ratio"),
                "zero_ratio": numeric.get("zero_ratio"),
            })

        payload = {
            "columns": columns_summary,
            "available_gap_skills": GAP_SKILLS,
        }
        result = llm_json(
            self.client,
            self.model,
            self.skills["gap_planner"],
            payload,
            label="gap_planner",
            timeline=self.timeline,
            rate_limiter=self.rate_limiter,
        )
        valid_columns = set(evidence["table"]["columns"])
        assignments = [
            a for a in (result.get("gap_assignments") or [])
            if a.get("column") in valid_columns and a.get("skill") in GAP_SKILLS
        ]
        return {"gap_assignments": assignments}

    def execute_column_interpretation_single(
        self,
        column: str,
        evidence: Dict[str, Any],
        semantic_type_result: Optional[Dict[str, Any]],
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        semantic_type_cols = (semantic_type_result or {}).get("columns", {})
        payload = {
            "table": evidence["table"],
            "target_column": column,
            "column_profile": evidence["column_profiles"][column],
            "raw_other_column_names": evidence["table"]["columns"],
            "semantic_type": semantic_type_cols.get(column),
            "revision_feedback": revision_feedback,
        }
        return llm_json(
            self.client,
            self.model,
            self.skills["column_interpretation"],
            payload,
            label=f"column_interpretation:{column}",
            timeline=self.timeline,
            rate_limiter=self.rate_limiter,
        )

    def execute_gap_skill(
        self,
        skill_name: str,
        column: str,
        evidence: Dict[str, Any],
        results: Dict[str, Any],
        reason: str = "",
    ) -> Dict[str, Any]:
        column_interp = (results.get("column_interpretation") or {}).get("columns", {})
        semantic_type = (results.get("semantic_type") or {}).get("columns", {})

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
            raise ValueError(f"알 수 없는 gap skill: {skill_name}")

        return llm_json(
            self.client,
            self.model,
            self.skills[skill_name],
            payload,
            label=f"{skill_name}:{column}",
            timeline=self.timeline,
            rate_limiter=self.rate_limiter,
        )

    def execute_semantic_validation_group(
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
            p for p in evidence.get("relation_evidence", {}).get("pairwise", [])
            if set(p.get("columns", [])) <= col_set
        ]
        grain_candidates = [
            g for g in evidence.get("grain_candidates", [])
            if set(g.get("columns", [])) <= col_set
        ]
        semantic_type_cols = (results.get("semantic_type") or {}).get("columns", {})
        column_interp_cols = (results.get("column_interpretation") or {}).get("columns", {})
        relation_analysis = results.get("relation_analysis") or {}
        relation_analysis_scoped = {
            "relations": [
                r for r in relation_analysis.get("relations", [])
                if set(r.get("columns", [])) <= col_set
            ],
            "revised_columns": {
                c: v for c, v in (relation_analysis.get("revised_columns") or {}).items() if c in col_set
            },
            "groups": [
                g for g in relation_analysis.get("groups", [])
                if set(g.get("columns", [])) <= col_set
            ],
        }

        payload = {
            "table": evidence["table"],
            "column_profiles": column_profiles,
            "relation_evidence": {"pairwise": pairwise},
            "grain_candidates": grain_candidates,
            "semantic_type": {"columns": {c: semantic_type_cols.get(c) for c in columns if c in semantic_type_cols}},
            "column_interpretation": {
                "columns": {c: column_interp_cols.get(c) for c in columns if c in column_interp_cols}
            },
            "relation_analysis": relation_analysis_scoped,
            "revision_feedback": revision_feedback,
        }
        return llm_json(
            self.client,
            self.model,
            self.skills["semantic_validation"],
            payload,
            label=f"semantic_validation:{group_label or '+'.join(columns)}",
            timeline=self.timeline,
            rate_limiter=self.rate_limiter,
        )

    def execute_skill(
        self,
        skill_name: str,
        evidence: Dict[str, Any],
        results: Dict[str, Any],
        focus: Optional[List[str]] = None,
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """semantic_type/relation_analysis/table_context 전용. column_interpretation은
        execute_column_interpretation_single(컬럼별 병렬)로, semantic_validation은
        execute_semantic_validation_group(그룹별 병렬)으로 각각 따로 처리한다."""

        if skill_name == "semantic_type":
            payload = {
                "table": evidence["table"],
                "column_profiles": evidence["column_profiles"],
                "raw_other_column_names": evidence["table"]["columns"],
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
            raise ValueError(f"execute_skill은 {skill_name}을 처리하지 않는다 (전용 메서드를 쓸 것)")

        return llm_json(
            self.client,
            self.model,
            self.skills[skill_name],
            payload,
            label=skill_name,
            timeline=self.timeline,
            rate_limiter=self.rate_limiter,
        )


# ---------------------------------------------------------------------
# 병렬 오케스트레이션 (컬럼별 column_interpretation, 그룹별 semantic_validation,
# gap skill 진단/실행) - run_pipeline이 이 함수들을 조합해서 쓴다.
# ---------------------------------------------------------------------

def run_column_interpretation_parallel(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    rate_limiter: RateLimiter,
    columns: List[str],
    revision_feedback: Optional[Dict[str, Any]] = None,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """columns 각각에 대해 column_interpretation을 병렬로 호출한다. existing이 있으면
    (재검증 라운드에서 focus로 좁힌 경우) 그 위에 columns만 덮어쓰고 나머지 컬럼은
    이전 라운드 결과를 그대로 유지한다."""
    semantic_type_result = results.get("semantic_type")
    merged: Dict[str, Any] = dict(existing or {})

    print(f"[PARALLEL] column_interpretation {len(columns)}개 컬럼 병렬 실행", flush=True)
    with ThreadPoolExecutor(max_workers=rate_limiter.max_concurrency) as pool:
        futures = {
            pool.submit(
                runner.execute_column_interpretation_single,
                col, evidence, semantic_type_result, revision_feedback,
            ): col
            for col in columns
        }
        for future in as_completed(futures):
            col = futures[future]
            merged[col] = future.result()

    return {"columns": merged}


def build_relation_groups(
    all_columns: List[str],
    relation_analysis_result: Optional[Dict[str, Any]],
    grain_candidates: List[Dict[str, Any]],
    pairwise_evidence: List[Dict[str, Any]],
) -> Tuple[List[List[str]], List[str]]:
    """관련 있는 컬럼들을 union-find로 묶는다. relation_analysis가 돌았으면 그 결과
    (통계+이름+의미까지 보고 LLM이 확정한 관계)를 쓰고, 안 돌았으면(플래너가 pairwise
    증거 없다고 판단해 애초에 relation_analysis를 안 돌린 경우) evidence의 pairwise
    통계만 쓴다 - 그 경우 강한 관계가 거의 없다는 뜻이라 대부분 단일 컬럼으로 남는 게
    맞다. grain_candidates(복합키 후보)도 같은 방식으로 묶는다."""
    parent: Dict[str, str] = {c: c for c in all_columns}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union_all(cols: List[str]) -> None:
        cols = [c for c in cols if c in parent]
        for c in cols[1:]:
            ra, rb = find(cols[0]), find(c)
            if ra != rb:
                parent[ra] = rb

    if relation_analysis_result:
        for g in relation_analysis_result.get("groups", []) or []:
            union_all(g.get("columns", []))
        for r in relation_analysis_result.get("relations", []) or []:
            union_all(r.get("columns", []))
    else:
        for pair in pairwise_evidence or []:
            union_all(pair.get("columns", []))

    for g in grain_candidates or []:
        cols = g.get("columns", [])
        if len(cols) > 1:
            union_all(cols)

    buckets: Dict[str, List[str]] = {}
    for c in all_columns:
        buckets.setdefault(find(c), []).append(c)

    groups = [cols for cols in buckets.values() if len(cols) > 1]
    ungrouped = [cols[0] for cols in buckets.values() if len(cols) == 1]
    return groups, ungrouped


def run_semantic_validation_grouped(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    rate_limiter: RateLimiter,
    revision_feedback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """테이블 전체를 한 번에 넣는 대신, relation_analysis가 확정한 관계 그룹별로
    쪼개서 병렬 검증한다. 어느 그룹에도 안 걸린 컬럼들은 하나의 배치로 묶어
    (컬럼 간 관계가 없으니 단일 컬럼 제약만 검증) 같이 병렬 실행한다."""
    all_columns = evidence["table"]["columns"]
    groups, ungrouped = build_relation_groups(
        all_columns,
        results.get("relation_analysis"),
        evidence.get("grain_candidates", []),
        evidence.get("relation_evidence", {}).get("pairwise", []),
    )

    jobs: List[Tuple[str, List[str]]] = [(f"group{i + 1}", g) for i, g in enumerate(groups)]
    if ungrouped:
        jobs.append(("ungrouped", ungrouped))

    print(
        f"[VALIDATE] {len(jobs)}개 단위 병렬 검증 "
        f"(관계그룹 {len(groups)}개, 단일컬럼 배치 "
        f"{'1개(' + str(len(ungrouped)) + '개 컬럼)' if ungrouped else '없음'})",
        flush=True,
    )

    outputs: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=rate_limiter.max_concurrency) as pool:
        futures = {
            pool.submit(
                runner.execute_semantic_validation_group,
                cols, evidence, results, revision_feedback, label,
            ): label
            for label, cols in jobs
        }
        for future in as_completed(futures):
            label = futures[future]
            outputs[label] = future.result()

    merged_checks: List[Dict[str, Any]] = []
    merged_requests: List[Dict[str, Any]] = []
    merged_validated: Dict[str, Any] = {}
    any_needs_revision = False
    for out in outputs.values():
        merged_checks.extend(out.get("checks", []) or [])
        merged_requests.extend(out.get("revision_requests", []) or [])
        merged_validated.update(out.get("validated_columns", {}) or {})
        if out.get("overall_status") == "needs_revision":
            any_needs_revision = True

    return {
        "overall_status": "needs_revision" if any_needs_revision else "pass",
        "checks": merged_checks,
        "revision_requests": merged_requests,
        "validated_columns": merged_validated,
    }


def run_gap_resolution(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    rate_limiter: RateLimiter,
) -> List[Dict[str, Any]]:
    """1차 해석(semantic_type/column_interpretation/relation_analysis) 직후 호출.
    gap_planner가 컬럼별 상태를 보고 GAP_SKILLS 중 뭘 붙일지 판단하면, 그 배정을
    병렬로 실행해서 column_interpretation.columns[col]에 반영한다."""
    diagnosis = runner.diagnose_gaps(evidence, results)
    assignments = diagnosis.get("gap_assignments", [])
    if not assignments:
        print("[GAP] 보충 필요한 컬럼 없음", flush=True)
        return []

    print(
        f"[GAP] {len(assignments)}건 보충 실행: "
        + ", ".join(f"{a['column']}->{a['skill']}" for a in assignments),
        flush=True,
    )

    outputs: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=rate_limiter.max_concurrency) as pool:
        futures = {
            pool.submit(
                runner.execute_gap_skill,
                a["skill"], a["column"], evidence, results, a.get("reason", ""),
            ): i
            for i, a in enumerate(assignments)
        }
        for future in as_completed(futures):
            outputs[futures[future]] = future.result()

    columns = results.setdefault("column_interpretation", {}).setdefault("columns", {})
    for i, a in enumerate(assignments):
        out = outputs.get(i)
        col = a["column"]
        if not isinstance(out, dict) or col not in columns:
            continue
        for key in ("selected_meaning", "status", "sparsity_reason", "note"):
            # None은 "이번엔 안 바꿈"이라는 뜻이라 - 있는 값을 null로 지워버리면 안 된다.
            if out.get(key) is not None:
                columns[col][key] = out[key]
        columns[col].setdefault("gap_history", []).append({
            "skill": a["skill"],
            "reason": a.get("reason", ""),
        })

    return assignments


def run_pipeline(
    csv_path: Path,
    skill_dir: Path,
    max_rounds: int = 2,
    checkpoint_path: Optional[Path] = None,
) -> Dict[str, Any]:
    run_started = time.time()
    run_started_at = now_iso()
    timeline: List[Dict[str, Any]] = []

    # 프로파일링/probe는 전부 pandas 연산이라 LLM 비용과 무관하다 - 행을 미리
    # 샘플링해서 넘기면 uniqueness/상관관계/probe 통과율 같은 통계 자체가 부정확해질
    # 뿐, LLM에 보내는 payload는 어차피 sample_values()가 컬럼당 12개로 이미
    # 고정돼 있어 줄지 않는다. 그래서 df를 따로 안 만들고 raw_df를 그대로 쓴다.
    raw_df = read_csv_safely(csv_path)

    print(f"[LOAD] {csv_path.name}: {len(raw_df):,} rows x {len(raw_df.columns)} cols", flush=True)

    profile_started_at = now_iso()
    profile_started = time.time()
    evidence = build_table_evidence(raw_df)
    profile_elapsed = time.time() - profile_started
    print(f"[PROFILE] 완료 ({profile_elapsed:.1f}초)", flush=True)
    timeline.append({
        "event": "profile",
        "started_at": profile_started_at,
        "finished_at": now_iso(),
        "elapsed_seconds": round(profile_elapsed, 3),
    })
    evidence["table"]["source_file"] = csv_path.name
    all_columns = evidence["table"]["columns"]

    client, model = make_client()
    rate_limiter = RateLimiter(
        requests_per_minute=int(os.environ.get("LLM_REQUESTS_PER_MINUTE", "360")),
        max_concurrency=int(os.environ.get("LLM_MAX_CONCURRENCY", "12")),
    )
    runner = SkillRunner(skill_dir, client, model, timeline=timeline, rate_limiter=rate_limiter)

    results: Dict[str, Any] = {}
    plans: List[Dict[str, Any]] = []

    def build_result(status: str, validation_status: str = "not_yet_run") -> Dict[str, Any]:
        return clean_for_json({
            "meta": {
                "source_csv": str(csv_path),
                "skills_dir": str(skill_dir),
                "llm_model": model,
                "max_rounds": max_rounds,
                "status": status,
                "validation_status": validation_status,
                "started_at": run_started_at,
                "finished_at": now_iso(),
                "elapsed_seconds": round(time.time() - run_started, 3),
            },
            "plans": plans,
            "evidence": evidence,
            "results": results,
            "timeline": timeline,
        })

    def save_checkpoint() -> None:
        if checkpoint_path is None:
            return
        checkpoint_path.write_text(
            json.dumps(build_result("in_progress"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def exec_step(skill: str, phase: str, round_idx: Optional[int] = None, **kwargs: Any) -> None:
        """semantic_type/relation_analysis/table_context 전용 - 단일 LLM 호출로 끝나는 skill."""
        label = f"[{'EXEC' if phase == 'exec' else 'RE-EXEC'}]"
        print(f"{label} {skill} 시작", flush=True)
        started_at = now_iso()
        started = time.time()
        results[skill] = runner.execute_skill(skill, evidence, results, **kwargs)
        elapsed = time.time() - started
        entry = {
            "event": "skill", "phase": phase, "skill": skill,
            "started_at": started_at, "finished_at": now_iso(),
            "elapsed_seconds": round(elapsed, 3),
        }
        if round_idx is not None:
            entry["round"] = round_idx
        timeline.append(entry)
        print(f"{label} {skill} 완료 ({elapsed:.1f}초)", flush=True)
        save_checkpoint()

    def exec_validation(phase: str, round_idx: Optional[int] = None, revision_feedback: Optional[Dict[str, Any]] = None) -> None:
        label = f"[{'EXEC' if phase == 'exec' else 'RE-EXEC'}]"
        print(f"{label} semantic_validation 시작", flush=True)
        started_at = now_iso()
        started = time.time()
        results["semantic_validation"] = run_semantic_validation_grouped(
            runner, evidence, results, rate_limiter, revision_feedback=revision_feedback,
        )
        probe_started_at = now_iso()
        probe_started = time.time()
        results["semantic_validation"] = apply_probes(raw_df, results["semantic_validation"])
        probe_elapsed = time.time() - probe_started
        print(f"[PROBE] semantic_validation 검증 완료 ({probe_elapsed:.1f}초)", flush=True)
        probe_entry = {
            "event": "probe", "skill": "semantic_validation",
            "started_at": probe_started_at, "finished_at": now_iso(),
            "elapsed_seconds": round(probe_elapsed, 3),
        }
        if round_idx is not None:
            probe_entry["round"] = round_idx
        timeline.append(probe_entry)
        elapsed = time.time() - started
        entry = {
            "event": "skill", "phase": phase, "skill": "semantic_validation",
            "started_at": started_at, "finished_at": now_iso(),
            "elapsed_seconds": round(elapsed, 3),
        }
        if round_idx is not None:
            entry["round"] = round_idx
        timeline.append(entry)
        print(f"{label} semantic_validation 완료 ({elapsed:.1f}초)", flush=True)
        save_checkpoint()

    # 1차 pass — 고정 뼈대(판단 여지 없음), LLM 계획 호출 없이 파이썬으로 순서를 정한다.
    # relation_analysis만 데이터 근거(pairwise 증거 유무)로 조건부 포함한다.
    has_pairwise = bool(evidence.get("relation_evidence", {}).get("pairwise"))
    first_pass_skills = ["semantic_type", "column_interpretation"]
    if has_pairwise:
        first_pass_skills.append("relation_analysis")
    print(
        "[PLAN] 1차 고정 순서:", " -> ".join(first_pass_skills),
        "" if has_pairwise else "(pairwise 증거 없어 relation_analysis 생략)",
        flush=True,
    )

    for skill in first_pass_skills:
        if skill == "column_interpretation":
            print(f"[EXEC] {skill} 시작", flush=True)
            step_started_at = now_iso()
            step_started = time.time()
            results[skill] = run_column_interpretation_parallel(
                runner, evidence, results, rate_limiter, columns=all_columns,
            )
            step_elapsed = time.time() - step_started
            timeline.append({
                "event": "skill", "phase": "exec", "skill": skill,
                "started_at": step_started_at, "finished_at": now_iso(),
                "elapsed_seconds": round(step_elapsed, 3),
            })
            print(f"[EXEC] {skill} 완료 ({step_elapsed:.1f}초)", flush=True)
            save_checkpoint()
        else:
            exec_step(skill, phase="exec")

    # Gap 진단 및 보충 — planner(gap_planner)가 컬럼별로 뭐가 부족한지 판단하고,
    # 해당하는 것만 병렬로 보충한다 (Sato식 2단계: 1차 독립 → 확정된 컬럼들을
    # 문맥으로 재조정).
    gap_started_at = now_iso()
    gap_started = time.time()
    gap_assignments = run_gap_resolution(runner, evidence, results, rate_limiter)
    gap_elapsed = time.time() - gap_started
    timeline.append({
        "event": "gap_resolution",
        "assignments": len(gap_assignments),
        "started_at": gap_started_at, "finished_at": now_iso(),
        "elapsed_seconds": round(gap_elapsed, 3),
    })
    save_checkpoint()

    # semantic_validation (관계 그룹별 병렬) → table_context (1차 마무리)
    exec_validation(phase="exec")
    exec_step("table_context", phase="exec")

    # Revision passes — 검증이 needs_revision이면 replan(기존 메커니즘)으로
    # 필요한 skill만 다시 돈다. max_rounds까지 반복해도 여전히 실패면 아래에서
    # meta.validation_status로 명시한다 - 실패한 채 "done"으로만 남기지 않는다.
    rounds_run = 1
    for round_idx in range(2, max_rounds + 1):
        rounds_run = round_idx
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
        replan = runner.plan(evidence, previous_results=results, validation_feedback=feedback)

        # A revision must end in validation; table context is refreshed after validation.
        skills_to_run = [s for s in replan.get("steps", []) if s["skill"] != "table_context"]
        if "semantic_validation" not in [s["skill"] for s in skills_to_run]:
            skills_to_run.append({
                "skill": "semantic_validation",
                "goal": "validate revised interpretation",
                "focus": [],
            })
        skills_to_run.sort(key=lambda x: SKILL_ORDER.index(x["skill"]))

        plans.append({"round": round_idx, "reason": replan.get("reason", ""), "steps": skills_to_run})
        print(f"[PLAN {round_idx}]", " -> ".join(step["skill"] for step in skills_to_run), flush=True)

        for step in skills_to_run:
            skill = step["skill"]
            if skill == "column_interpretation":
                focus_cols = step.get("focus") or all_columns
                existing = (results.get("column_interpretation") or {}).get("columns", {})
                print(f"[RE-EXEC] {skill} 시작 ({len(focus_cols)}개 컬럼)", flush=True)
                step_started_at = now_iso()
                step_started = time.time()
                results[skill] = run_column_interpretation_parallel(
                    runner, evidence, results, rate_limiter,
                    columns=focus_cols, revision_feedback=feedback, existing=existing,
                )
                step_elapsed = time.time() - step_started
                timeline.append({
                    "event": "skill", "phase": "re-exec", "skill": skill, "round": round_idx,
                    "started_at": step_started_at, "finished_at": now_iso(),
                    "elapsed_seconds": round(step_elapsed, 3),
                })
                print(f"[RE-EXEC] {skill} 완료 ({step_elapsed:.1f}초)", flush=True)
                save_checkpoint()
            elif skill == "semantic_validation":
                exec_validation(phase="re-exec", round_idx=round_idx, revision_feedback=feedback)
            else:
                exec_step(skill, phase="re-exec", round_idx=round_idx, focus=step.get("focus", []), revision_feedback=feedback)

        # Always regenerate final table context after a revision.
        exec_step("table_context", phase="re-exec", round_idx=round_idx, revision_feedback=feedback)

    final_validation = results.get("semantic_validation") or {}
    validation_status = "pass"
    if final_validation.get("overall_status") == "needs_revision":
        unresolved = [
            c for c in final_validation.get("checks", []) if c.get("status") in {"warning", "fail"}
        ]
        validation_status = "unresolved_after_max_rounds"
        print(
            f"[VALIDATION] max_rounds({max_rounds}) 도달 - {rounds_run}라운드까지 돌았지만 "
            f"여전히 needs_revision. 미해결 {len(unresolved)}건: "
            + "; ".join((c.get("hypothesis") or "")[:60] for c in unresolved[:5]),
            flush=True,
        )

    final = build_result("done", validation_status=validation_status)
    if checkpoint_path is not None and checkpoint_path.exists():
        checkpoint_path.unlink()
    return final


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
    checkpoint_path = output.with_suffix(output.suffix + ".partial.json")

    result = run_pipeline(
        csv_path=csv_path,
        skill_dir=args.skills.resolve(),
        max_rounds=args.max_rounds,
        checkpoint_path=checkpoint_path,
    )

    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[DONE] {output}")


if __name__ == "__main__":
    main()
