"""컬럼 단위 프로파일. LLM에 넘어가는 모든 수치의 출처.

여기서 추정값을 만들어내지 않는다 - 이 출력이 이후 모든 판단의 근거 집합이라
오염되면 그대로 전파된다. 판정이 서지 않으면 값을 지어내는 대신 None을 낸다
(numeric_profile/datetime_profile이 파싱 비율 임계 미달 시 None을 내는 이유).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from column_semantics.core.jsonx import json_safe
from column_semantics.core.naming import split_tokens


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
                [
                    json_safe(v)
                    for v in pd.to_numeric(s, errors="coerce").dropna().drop_duplicates().tolist()
                ]
            )[:20]

        out[str(col)] = profile

    return out
