"""numeric-measure handler. 단위 hallucination 차단이 핵심."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import SkillContext, SkillDeps, SkillResult
from agent.skill_utils import column_fact, messages, semantic_result

Evidence = Literal["column_name", "source_description", "sample_value", "not_found"]


class MeasureOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str = Field(description="무엇을 측정한 값인지 1~2문장")
    unit: str = Field(default="", description="근거가 있을 때만. 없으면 빈 문자열")
    unit_evidence: Evidence
    usage: str = Field(default="", description="어떤 분석에 쓰이는지")
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    out: MeasureOut = await deps.structured(
        messages(ctx, "이 수치가 무엇을 측정한 값이고 어떤 용도로 쓰이는지 판단하라."),
        MeasureOut,
        stage="column",
    )

    # unit과 unit_evidence의 정합성. 스키마로는 강제할 수 없는 교차 제약이다.
    if out.unit and out.unit_evidence == "not_found":
        return SkillResult(
            skill="numeric-measure", ok=False,
            notes=f"unit='{out.unit}'인데 unit_evidence=not_found. 근거가 없으면 unit을 비워라.",
        )

    if out.unit:
        f = column_fact(ctx, ctx.target)
        haystack = " ".join(
            [ctx.target, ctx.asset_name, ctx.source_description] + f.get("samples", [])
        ).lower()
        if out.unit.lower() not in haystack:
            return SkillResult(
                skill="numeric-measure", ok=False,
                notes=f"입력 어디에도 없는 단위 '{out.unit}'. 컬럼명/설명/표본값에 있는 단위만 사용하라.",
            )

    return semantic_result(
        "numeric-measure",
        ctx,
        {"kind": "numeric", "meaning": out.meaning, "unit": out.unit, "usage": out.usage},
        out.confidence,
    )
