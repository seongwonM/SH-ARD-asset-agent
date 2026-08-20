"""컬럼 사이의 관계 증거. 값 자체에서만 나오는 통계라 LLM 판단이 섞이지 않는다."""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


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

        # Value overlap (inclusion dependency). 한쪽 값 집합이 다른 쪽에 담기는지는
        # 코드-마스터, 부모-자식, 같은 개념의 다른 표기를 가르는 가장 직접적인
        # 증거인데 지금까지 계산하지 않았다 - 그래서 관계 해석이 상관계수와
        # 시간 순서에만 기대고 있었다. 행 기준과 고유값 기준을 같이 낸다:
        # 행 기준은 "실제로 얼마나 덮이는가", 고유값 기준은 "값의 종류가 담기는가"다.
        if av.nunique() <= 5000 and bv.nunique() <= 5000:
            a_str, b_str = av.astype(str), bv.astype(str)
            a_set, b_set = set(a_str.unique()), set(b_str.unique())
            shared = a_set & b_set
            a_in_b_rows = float(a_str.isin(b_set).mean())
            b_in_a_rows = float(b_str.isin(a_set).mean())
            if max(a_in_b_rows, b_in_a_rows) >= 0.5:
                evidence["value_overlap"] = {
                    "a_in_b_row_ratio": a_in_b_rows,
                    "b_in_a_row_ratio": b_in_a_rows,
                    "a_in_b_unique_ratio": len(shared) / len(a_set) if a_set else 0.0,
                    "b_in_a_unique_ratio": len(shared) / len(b_set) if b_set else 0.0,
                    "a_unique": int(len(a_set)),
                    "b_unique": int(len(b_set)),
                    "shared_unique": int(len(shared)),
                }

        # Mapping consistency for hierarchy / functional dependency.
        # b -> a: for each b, does it map to one a?
        if av.nunique() <= 5000 and bv.nunique() <= 5000:
            try:
                b_to_a = pd.DataFrame({"a": av.astype(str), "b": bv.astype(str)}).drop_duplicates()
                cnt = b_to_a.groupby("b")["a"].nunique()
                if len(cnt):
                    weighted_ok = float(bv.astype(str).isin(cnt[cnt == 1].index).mean())
                    if weighted_ok >= 0.9:
                        evidence["b_to_a_mapping_consistency"] = weighted_ok

                a_to_b = b_to_a.groupby("a")["b"].nunique()
                if len(a_to_b):
                    weighted_ok = float(av.astype(str).isin(a_to_b[a_to_b == 1].index).mean())
                    if weighted_ok >= 0.9:
                        evidence["a_to_b_mapping_consistency"] = weighted_ok
            except Exception:
                pass

        if len(evidence) > 1:
            relations.append(evidence)

    return {"pairwise": relations}


def find_grain_candidates(
    df: pd.DataFrame, max_width: int = 3, max_cols: int = 20
) -> List[Dict[str, Any]]:
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


def build_relation_groups(
    all_columns: List[str],
    relation_analysis_result: Optional[Dict[str, Any]],
    grain_candidates: List[Dict[str, Any]],
    pairwise_evidence: List[Dict[str, Any]],
) -> Tuple[List[List[str]], List[str]]:
    """관련 있는 컬럼들을 union-find로 묶는다. relation_analysis가 돌았으면 그 결과
    (통계+이름+의미까지 보고 LLM이 확정한 관계)를 쓰고, 안 돌았으면(pairwise 증거가
    없어 애초에 relation_analysis를 건너뛴 경우) evidence의 pairwise 통계만 쓴다 -
    그 경우 강한 관계가 거의 없다는 뜻이라 대부분 단일 컬럼으로 남는 게 맞다.
    grain_candidates(복합키 후보)도 같은 방식으로 묶는다."""
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
