"""distribution-profile handler. 분포 특성을 결정론적으로 계산한다."""

from __future__ import annotations

import pandas as pd

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    df = deps.dataframe(ctx.data_ref)
    contributions = []

    for col in profile.get("columns", []):
        name = col["name"]
        kind = col.get("kind", "")
        series = df[name].dropna()
        if not len(series) or kind not in {"numeric", "categorical"}:
            continue

        if kind == "numeric":
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            if not len(numeric):
                continue
            value = {
                "distribution_type": "numeric",
                "zero_ratio": round(float((numeric == 0).mean()), 4),
                "negative_ratio": round(float((numeric < 0).mean()), 4),
                "is_constant": bool(numeric.nunique() <= 1),
                "quantiles": {
                    "p25": float(numeric.quantile(0.25)),
                    "p50": float(numeric.quantile(0.5)),
                    "p75": float(numeric.quantile(0.75)),
                },
            }
        else:
            counts = series.astype(str).value_counts(dropna=False)
            top = counts.head(5)
            total = int(counts.sum())
            value = {
                "distribution_type": "categorical",
                "top_values": [
                    {
                        "value": str(idx),
                        "count": int(cnt),
                        "ratio": round(int(cnt) / total, 4) if total else 0.0,
                    }
                    for idx, cnt in top.items()
                ],
                "dominant_value_ratio": round(int(top.iloc[0]) / total, 4) if total and len(top) else 0.0,
                "is_single_value": bool(len(counts) == 1),
            }

        contributions.append(
            Contribution(
                slot=Slot.DISTRIBUTION_PROFILE,
                key=name,
                value=value,
                evidence=["dataframe distribution scan"],
                confidence=1.0,
            )
        )

    return SkillResult(
        skill="distribution-profile",
        contributions=contributions,
        notes=f"{len(contributions)}개 컬럼 분포 요약",
    )
