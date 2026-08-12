"""profile-table handler. LLM을 쓰지 않는 결정론적 skill."""

from __future__ import annotations

import re

from agent.contract import ColumnKind, Contribution, SkillContext, SkillDeps, SkillResult, Slot

SPATIAL_PAT = re.compile(r"(lat|lon|lng|geo|region|site|line|zone|addr)", re.I)
ID_SUFFIX = ("_id", "_key", "_no", "_code")


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    import pandas as pd

    df = deps.dataframe(ctx.data_ref)
    rows = int(len(df))
    facts = []

    for name in df.columns:
        s = df[name]
        nn = s.dropna()
        distinct = int(nn.nunique())
        ratio = distinct / rows if rows else 0.0
        lengths = nn.astype(str).str.len()
        avg_len = float(lengths.mean()) if len(lengths) else 0.0

        dt_ratio = _datetime_ratio(nn, s)
        kind = _classify(str(name), s, ratio, avg_len, dt_ratio)

        lo, hi = "", ""
        if kind == ColumnKind.TEMPORAL and len(nn):
            parsed = pd.to_datetime(nn, errors="coerce").dropna()
            if len(parsed):
                lo, hi = parsed.min().strftime("%Y-%m-%d"), parsed.max().strftime("%Y-%m-%d")
        elif kind == ColumnKind.NUMERIC and len(nn):
            lo, hi = str(nn.min()), str(nn.max())

        facts.append(
            {
                "name": str(name),
                "kind": kind.value,
                "dtype": str(s.dtype),
                "distinct_count": distinct,
                "distinct_ratio": round(ratio, 4),
                "null_ratio": round(float(s.isna().mean()) if rows else 0.0, 4),
                "avg_char_length": round(avg_len, 2),
                "min_value": lo,
                "max_value": hi,
                "samples": [str(v) for v in nn.head(5).tolist()],
            }
        )

    return SkillResult(
        skill="profile-table",
        contributions=[
            Contribution(
                slot=Slot.TABLE_PROFILE,
                value={"row_count": rows, "column_count": len(facts), "columns": facts},
                evidence=["pandas dtype 및 분포 통계"],
                confidence=1.0,
            )
        ],
        notes=f"{len(facts)}개 컬럼 프로파일링",
    )


def _datetime_ratio(nn, series) -> float:
    """
    수치 dtype에는 datetime 파싱을 시도하지 않는다.

    pd.to_datetime(501)은 나노초 epoch으로 해석되어 성공한다. 이 때문에
    power_value 같은 정수 측정값이 파싱 성공률 1.0을 받아 temporal로 오분류되고,
    시간축 skill이 배정되는 문제가 실제로 발생했다.
    """
    import pandas as pd

    if not len(nn) or pd.api.types.is_numeric_dtype(series):
        return 0.0
    try:
        return float(pd.to_datetime(nn, errors="coerce", format="mixed").notna().mean())
    except (TypeError, ValueError):
        return 0.0


def _classify(name, series, ratio, avg_len, dt_ratio) -> ColumnKind:
    import pandas as pd

    low = name.lower()
    if pd.api.types.is_datetime64_any_dtype(series) or dt_ratio >= 0.9:
        return ColumnKind.TEMPORAL
    if low.endswith(ID_SUFFIX) or low == "id" or ratio >= 0.98:
        return ColumnKind.IDENTIFIER
    if SPATIAL_PAT.search(low):
        return ColumnKind.SPATIAL
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return ColumnKind.NUMERIC
    if avg_len >= 40:
        return ColumnKind.FREE_TEXT
    distinct = series.dropna().nunique()
    rows = len(series)
    # distinct_ratio 임계값만 쓰면 행이 적을 때 무너진다. 4행짜리 표본에서
    # 값이 2종류면 ratio가 0.5라 categorical 판정에 걸리지 않고 UNKNOWN으로 빠진다.
    # 행이 적을 때는 절대 개수로, 많을 때는 비율로 판정한다.
    if 0 < distinct <= 30 and (rows < 100 or ratio <= 0.05):
        return ColumnKind.CATEGORICAL
    return ColumnKind.UNKNOWN
