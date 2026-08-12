"""generic-column handler. 전용 skill이 없는 컬럼용 fallback."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import SkillContext, SkillDeps, SkillResult
from agent.skill_utils import messages, semantic_result


class GenericOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str = Field(description="이 컬럼이 무엇을 담는지 1~2문장")
    usage: str = Field(default="", description="어떤 용도로 보이는지. 불확실하면 빈 문자열")
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    out: GenericOut = await deps.structured(
        messages(ctx, "이 컬럼이 무엇을 담고 있는지 표본값에 근거해 판단하라."),
        GenericOut,
        stage="column",
    )
    return semantic_result(
        "generic-column",
        ctx,
        {"kind": "generic", "meaning": out.meaning, "usage": out.usage},
        out.confidence,
    )
