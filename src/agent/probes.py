"""
Probe: 데이터를 직접 조회해 주장을 반증하는 결정론적 검사.

왜 필요한가.
guards.py는 텍스트를 텍스트와 대조한다. "입력에 kPa가 없다"는 잡지만,
"이 컬럼이 primary key다"라는 주장은 잡지 못한다. 그 주장의 진위는
텍스트가 아니라 데이터에 있기 때문이다.

probe는 LLM이 한 주장에 대해 "이 검사를 통과하지 못하면 그 주장은 거짓"인
반증 조건을 데이터로 실행한다. Popper식 반증이지 확인이 아니다 —
통과했다고 참이 되는 게 아니라, 실패하면 확실히 거짓이다.

skill은 검사 가능한 주장을 할 때 반드시 probe를 첨부한다(contract.VerifiableClaim).
executor가 자동으로 실행하고, 실패하면 probe의 실측값이 재시도 힌트가 된다.
LLM이 만든 힌트보다 데이터에서 나온 힌트가 교정에 훨씬 강하다.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class ProbeKind(str, Enum):
    UNIQUENESS = "uniqueness"  # 단일/복합 컬럼의 유일성
    NULL_RATE = "null_rate"
    VALUE_IN_SET = "value_in_set"  # 실제 값이 선언된 집합 안에 있는가
    SET_COVERS_COLUMN = "set_covers_column"  # 선언 집합이 실제 값을 모두 덮는가
    NUMERIC_RANGE = "numeric_range"
    TIME_COMPONENT = "time_component"  # 시:분:초 성분이 실제로 존재하는가
    REGEX_MATCH = "regex_match"
    DISTINCT_COUNT = "distinct_count"
    FUNCTIONAL_DEP = "functional_dep"  # A -> B 함수 종속 (X가 정해지면 Y가 하나)


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProbeKind
    columns: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProbeKind
    columns: List[str] = Field(default_factory=list)
    passed: bool = False
    observed: Any = None
    detail: str = ""  # 재시도 힌트로 그대로 쓰인다. 실측값을 포함할 것.
    error: str = ""


# ---------------------------------------------------------------------------


def run_probe(req: ProbeRequest, df) -> ProbeResult:
    """probe 1건 실행. 실행 자체가 실패하면 passed=False가 아니라 error를 채운다."""
    try:
        fn = _DISPATCH.get(req.kind)
        if fn is None:
            return ProbeResult(kind=req.kind, columns=req.columns, error=f"미구현 probe: {req.kind}")
        missing = [c for c in req.columns if c not in df.columns]
        if missing:
            return ProbeResult(
                kind=req.kind, columns=req.columns, passed=False,
                detail=f"존재하지 않는 컬럼: {missing}. 실제 컬럼은 {list(df.columns)}이다.",
            )
        return fn(req, df)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(kind=req.kind, columns=req.columns, error=f"{type(exc).__name__}: {exc}"[:200])


def run_probes(reqs: List[ProbeRequest], df) -> List[ProbeResult]:
    return [run_probe(r, df) for r in reqs]


# ---------------------------------------------------------------------------
# 구현
# ---------------------------------------------------------------------------


def _uniqueness(req: ProbeRequest, df) -> ProbeResult:
    """단일 또는 복합 컬럼의 유일성. grain 판정과 PK 주장의 반증에 쓰인다."""
    threshold = float(req.params.get("min_ratio", 0.99))
    sub = df[req.columns].dropna()
    total = len(sub)
    if total == 0:
        return ProbeResult(kind=req.kind, columns=req.columns, passed=False, observed=0.0,
                           detail="비어 있는 컬럼이라 유일성을 확인할 수 없다.")
    distinct = len(sub.drop_duplicates())
    ratio = distinct / total
    passed = ratio >= threshold
    dup_note = ""
    if not passed:
        dups = sub[sub.duplicated(keep=False)]
        if len(dups):
            sample = dups.head(2).to_dict("records")
            dup_note = f" 중복 예시: {sample}"
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=passed, observed=round(ratio, 4),
        detail=f"{req.columns}의 실제 유일성 비율은 {ratio:.4f}이다(기준 {threshold}).{dup_note}",
    )


def _null_rate(req: ProbeRequest, df) -> ProbeResult:
    threshold = float(req.params.get("max_rate", 0.0))
    col = req.columns[0]
    rate = float(df[col].isna().mean()) if len(df) else 0.0
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=rate <= threshold, observed=round(rate, 4),
        detail=f"{col}의 실제 결측 비율은 {rate:.4f}이다(허용 {threshold}).",
    )


def _value_in_set(req: ProbeRequest, df) -> ProbeResult:
    """선언한 값들이 실제 데이터에 존재하는가. 관측되지 않은 카테고리 설명을 반증한다."""
    col = req.columns[0]
    declared = {str(v).strip().lower() for v in req.params.get("values", [])}
    actual = {str(v).strip().lower() for v in df[col].dropna().unique()}
    unseen = sorted(declared - actual)
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=not unseen, observed=unseen,
        detail=(
            f"{col}에 실제로 존재하지 않는 값을 설명했다: {unseen}. "
            f"실제 값은 {sorted(actual)[:20]}이다."
        ) if unseen else f"{col}의 선언 값이 모두 실제 데이터에 존재한다.",
    )


def _set_covers_column(req: ProbeRequest, df) -> ProbeResult:
    """실제 값 중 설명되지 않은 것이 있는가. 누락 경고용(critical=False 권장)."""
    col = req.columns[0]
    declared = {str(v).strip().lower() for v in req.params.get("values", [])}
    actual = {str(v).strip().lower() for v in df[col].dropna().unique()}
    missing = sorted(actual - declared)
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=not missing, observed=missing,
        detail=f"{col}에서 설명되지 않은 실제 값: {missing}" if missing else f"{col}의 모든 값이 설명됐다.",
    )


def _numeric_range(req: ProbeRequest, df) -> ProbeResult:
    import pandas as pd

    col = req.columns[0]
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if not len(s):
        return ProbeResult(kind=req.kind, columns=req.columns, passed=False,
                           detail=f"{col}에서 수치 값을 얻지 못했다.")
    lo, hi = float(s.min()), float(s.max())
    d_lo, d_hi = req.params.get("min"), req.params.get("max")
    ok = True
    if d_lo is not None and abs(float(d_lo) - lo) > 1e-9:
        ok = False
    if d_hi is not None and abs(float(d_hi) - hi) > 1e-9:
        ok = False
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=ok, observed=[lo, hi],
        detail=f"{col}의 실제 범위는 {lo} ~ {hi}이다(주장 {d_lo} ~ {d_hi}).",
    )


def _time_component(req: ProbeRequest, df) -> ProbeResult:
    """
    시/분/초 성분이 실제로 존재하는가.
    "이 컬럼은 Hour 해상도다"라는 주장을 데이터로 반증한다.
    """
    import pandas as pd

    col = req.columns[0]
    need = str(req.params.get("resolution", "Day"))
    parsed = pd.to_datetime(df[col], errors="coerce").dropna()
    if not len(parsed):
        return ProbeResult(kind=req.kind, columns=req.columns, passed=False,
                           detail=f"{col}을 시각으로 파싱하지 못했다.")

    has_h = bool((parsed.dt.hour != 0).any())
    has_m = bool((parsed.dt.minute != 0).any())
    has_s = bool((parsed.dt.second != 0).any())
    finest = "Second" if has_s else "Minute" if has_m else "Hour" if has_h else "Day"

    order = ["Year", "Quarter", "Month", "Week", "Day", "Hour", "Minute", "Second"]
    try:
        passed = order.index(need) <= order.index(finest)
    except ValueError:
        passed = False
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=passed, observed=finest,
        detail=(
            f"{col}에서 실제로 구분 가능한 최소 단위는 {finest}이다. "
            f"{need} 해상도를 주장할 근거가 데이터에 없다."
        ) if not passed else f"{col}의 실제 최소 단위는 {finest}이다.",
    )


def _regex_match(req: ProbeRequest, df) -> ProbeResult:
    col = req.columns[0]
    pattern = req.params.get("pattern", "")
    threshold = float(req.params.get("min_ratio", 0.9))
    s = df[col].dropna().astype(str)
    if not len(s):
        return ProbeResult(kind=req.kind, columns=req.columns, passed=False, detail=f"{col}이 비어 있다.")
    ratio = float(s.str.match(pattern).mean())
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=ratio >= threshold, observed=round(ratio, 4),
        detail=f"{col}에서 패턴 '{pattern}' 일치 비율은 {ratio:.4f}이다(기준 {threshold}).",
    )


def _distinct_count(req: ProbeRequest, df) -> ProbeResult:
    col = req.columns[0]
    actual = int(df[col].dropna().nunique())
    declared = req.params.get("expected")
    passed = declared is None or int(declared) == actual
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=passed, observed=actual,
        detail=f"{col}의 실제 고유값 개수는 {actual}이다(주장 {declared}).",
    )


def _functional_dep(req: ProbeRequest, df) -> ProbeResult:
    """
    A -> B 함수 종속. columns=[A..., B]. 마지막이 종속 대상.
    "equipment_id가 정해지면 line_id도 정해진다" 같은 주장의 반증에 쓴다.
    """
    *lhs, rhs = req.columns
    sub = df[lhs + [rhs]].dropna()
    if not len(sub):
        return ProbeResult(kind=req.kind, columns=req.columns, passed=False, detail="검사할 행이 없다.")
    grouped = sub.groupby(lhs)[rhs].nunique()
    violations = grouped[grouped > 1]
    passed = len(violations) == 0
    return ProbeResult(
        kind=req.kind, columns=req.columns, passed=passed, observed=int(len(violations)),
        detail=(
            f"{lhs} → {rhs} 함수 종속이 성립하지 않는다. "
            f"위반 그룹 {len(violations)}건(예: {violations.head(2).to_dict()})."
        ) if not passed else f"{lhs} → {rhs} 함수 종속이 성립한다.",
    )


_DISPATCH = {
    ProbeKind.UNIQUENESS: _uniqueness,
    ProbeKind.NULL_RATE: _null_rate,
    ProbeKind.VALUE_IN_SET: _value_in_set,
    ProbeKind.SET_COVERS_COLUMN: _set_covers_column,
    ProbeKind.NUMERIC_RANGE: _numeric_range,
    ProbeKind.TIME_COMPONENT: _time_component,
    ProbeKind.REGEX_MATCH: _regex_match,
    ProbeKind.DISTINCT_COUNT: _distinct_count,
    ProbeKind.FUNCTIONAL_DEP: _functional_dep,
}
