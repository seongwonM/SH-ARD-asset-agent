"""
채점.

측정을 두 종류로 엄격히 나눈다.

  A. 정답 대비 정확도 (make_mock_data.py의 truth.json)
     컬럼 kind, 식별자 role, 입도 키, 시간 해상도, PII, 함수 종속.
     이건 생성 시점에 알고 있던 사실이므로 채점 가능하다.

  B. 정답 없이 재는 것
     probe 반증률, 반복 일관성, 커버리지, 비용.
     자연어 요약의 "좋음"은 여기서 재지 않는다 - 잴 방법이 없다.

**B를 A인 것처럼 보고하지 않는다.** 반복 일관성이 높다고 정확한 게 아니다.
같은 오답을 20번 반복해도 일관성은 1.0이다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# A. 정답 대비
# ---------------------------------------------------------------------------


def score_against_truth(result: Dict[str, Any], truth: Dict[str, Any]) -> Dict[str, Any]:
    ac = result.get("asset_context") or {}
    cols = {c["name"]: c for c in ac.get("columns", [])}

    out: Dict[str, Any] = {}

    # 1. 컬럼 kind — 결정론적 프로파일러의 산출물. 1.0이 아니면 규칙 버그다.
    kind_truth = truth.get("kinds", {})
    if kind_truth:
        hits, misses = 0, []
        profile_kinds = _kinds_from_result(result)
        for name, expected in kind_truth.items():
            actual = profile_kinds.get(name)
            if actual == expected:
                hits += 1
            else:
                misses.append(f"{name}: {actual}≠{expected}")
        out["kind_accuracy"] = round(hits / len(kind_truth), 3)
        out["kind_misses"] = misses

    # 2. 식별자 role — LLM 판정 + probe 검증
    role_truth = truth.get("identifier_roles", {})
    if role_truth:
        linkage = {l["column"]: l for l in ac.get("linkage", [])}
        hits, misses = 0, []
        for name, expected in role_truth.items():
            actual = (linkage.get(name) or {}).get("role")
            if actual == expected:
                hits += 1
            else:
                misses.append(f"{name}: {actual}≠{expected}")
        out["role_accuracy"] = round(hits / len(role_truth), 3)
        out["role_misses"] = misses

    # 3. 입도 키 — 집합 일치
    grain_truth = set(truth.get("grain_keys", []))
    if grain_truth:
        grain_actual = set(_grain_keys(result))
        out["grain_exact"] = int(grain_actual == grain_truth)
        out["grain_actual"] = sorted(grain_actual)

    # 4. 시간 해상도
    res_truth = truth.get("time_resolution", {})
    if res_truth:
        hits, misses = 0, []
        for name, expected in res_truth.items():
            actual = (cols.get(name) or {}).get("resolution")
            if actual == expected:
                hits += 1
            else:
                misses.append(f"{name}: {actual}≠{expected}")
        out["resolution_accuracy"] = round(hits / len(res_truth), 3)
        out["resolution_misses"] = misses

    # 5. PII — 정밀도/재현율. 미탐이 과탐보다 비싸므로 둘을 따로 본다.
    pii_truth = {k for k, v in (truth.get("pii") or {}).items() if v == "direct"}
    pii_pred = {
        c["column"]
        for c in ((ac.get("compliance") or {}).get("pii_columns") or [])
        if c.get("level") == "direct"
    }
    if pii_truth or pii_pred:
        tp = len(pii_truth & pii_pred)
        out["pii_recall"] = round(tp / len(pii_truth), 3) if pii_truth else None
        out["pii_precision"] = round(tp / len(pii_pred), 3) if pii_pred else None
        out["pii_missed"] = sorted(pii_truth - pii_pred)
        out["pii_false_positive"] = sorted(pii_pred - pii_truth)

    # 6. 함수 종속 — 결정론적. 재현율만 본다(전수 검사라 과탐이 없다)
    fd_truth = {tuple(x) for x in (truth.get("functional_dependencies") or [])}
    if fd_truth:
        fd_pred = {
            (d["determinant"], d["dependent"])
            for d in ((ac.get("constraints") or {}).get("functional_dependencies") or [])
        }
        out["fd_recall"] = round(len(fd_truth & fd_pred) / len(fd_truth), 3)
        out["fd_missed"] = sorted(fd_truth - fd_pred)

    return out


def _kinds_from_result(result: Dict[str, Any]) -> Dict[str, str]:
    """
    프로파일러가 판정한 kind로 채점한다.

    skill이 붙이는 `kind`는 skill 이름 기반이라(free_text 컬럼도 generic-column이
    처리하면 "generic") 프로파일러 판정과 다르다. 여기서 재려는 것은
    결정론적 분류 규칙의 정확도이므로 `profiled_kind`를 봐야 한다.
    """
    ac = result.get("asset_context") or {}
    return {c["name"]: c.get("profiled_kind", "") for c in ac.get("columns", [])}


def _grain_keys(result: Dict[str, Any]) -> List[str]:
    return (result.get("asset_context") or {}).get("grain_keys") or []


# ---------------------------------------------------------------------------
# B. 정답 없이
# ---------------------------------------------------------------------------


def score_process(result: Dict[str, Any]) -> Dict[str, Any]:
    perf = result.get("performance", {})
    ac = result.get("asset_context") or {}
    ver = ac.get("verification", {})
    trace = result.get("trace", [])

    acts = [t for t in trace if t.get("phase") != "plan" and t.get("skill")]
    retried = [t for t in acts if (t.get("attempts") or 1) > 1]
    failed = [t for t in acts if t.get("ok") is False]

    total_cols = len(ac.get("columns", [])) + len(perf.get("blocked", []))
    return {
        "elapsed_seconds": perf.get("elapsed_seconds", 0.0),
        "llm_calls": perf.get("llm_call_count", 0),
        "total_tokens": perf.get("llm_total_tokens", 0),
        "probe_runs": perf.get("probe_runs", 0),
        "iterations": perf.get("iterations", 0),
        # 1차 시도에서 반증/가드에 걸린 비율. 높으면 프롬프트가 약한 것,
        # 0이면 probe가 느슨한 것일 수 있다. 양쪽 다 신호다.
        "retry_rate": round(len(retried) / len(acts), 3) if acts else 0.0,
        "failed_tasks": len(failed),
        "blocked": perf.get("blocked", []),
        "column_coverage": round(len(ac.get("columns", [])) / total_cols, 3) if total_cols else 0.0,
        "probe_coverage": ver.get("probe_coverage", 0.0),
        "refuted_count": len(ver.get("refuted", [])),
        "unverified_count": ver.get("unverified_count", 0),
        "issues": len(result.get("issues", [])),
        "stop_reason": next(
            (t["note"] for t in reversed(trace) if t.get("phase") == "plan" and t.get("note")), ""
        )[:80],
    }


# ---------------------------------------------------------------------------
# 반복 일관성
# ---------------------------------------------------------------------------


def consistency(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    같은 입력 N회 결과의 일치도.

    주의: 일관성은 정확도가 아니다. 같은 오답을 N번 내도 1.0이다.
    정확도(A)와 반드시 함께 봐야 의미가 있다.
    """
    if len(results) < 2:
        return {}

    def roles(r):
        ac = r.get("asset_context") or {}
        return {l["column"]: l.get("role") for l in ac.get("linkage", [])}

    def resolutions(r):
        ac = r.get("asset_context") or {}
        return {c["name"]: c.get("resolution") for c in ac.get("columns", []) if c.get("resolution")}

    def keywords(r):
        ac = r.get("asset_context") or {}
        return set(ac.get("asset_context_details", {}).get("keywords", []))

    def summary_tokens(r):
        ac = r.get("asset_context") or {}
        return set((ac.get("asset_context_details", {}).get("summary") or "").split())

    out = {
        "role_agreement": _majority_agreement([roles(r) for r in results]),
        "resolution_agreement": _majority_agreement([resolutions(r) for r in results]),
        "keyword_jaccard": _mean_pairwise_jaccard([keywords(r) for r in results]),
        "summary_jaccard": _mean_pairwise_jaccard([summary_tokens(r) for r in results]),
    }
    return {k: v for k, v in out.items() if v is not None}


def _majority_agreement(dicts: List[Dict[str, Any]]) -> float | None:
    """항목별로 최빈값과 일치하는 비율의 평균."""
    keys = set().union(*dicts) if dicts else set()
    if not keys:
        return None
    scores = []
    for k in keys:
        vals = [d.get(k) for d in dicts]
        top = Counter(vals).most_common(1)[0][1]
        scores.append(top / len(vals))
    return round(sum(scores) / len(scores), 3)


def _mean_pairwise_jaccard(sets: List[set]) -> float | None:
    pairs = [
        (a, b) for i, a in enumerate(sets) for b in sets[i + 1 :]
    ]
    if not pairs:
        return None
    vals = []
    for a, b in pairs:
        union = a | b
        vals.append(len(a & b) / len(union) if union else 1.0)
    return round(sum(vals) / len(vals), 3)
