"""템플릿 handler. 복사해서 시작한다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import SkillContext, SkillDeps, SkillResult
from agent.skill_utils import column_fact, messages, semantic_result

# vLLM guided decoding 제약:
#   Optional/Union 금지, 라벨은 Literal로 고정, 중첩 2단계 이내, extra="forbid"
SomeLabel = Literal["a", "b", "unknown"]


class MyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str = Field(description="핵심 해석 1~2문장")
    label: SomeLabel
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    out: MyOut = await deps.structured(
        messages(ctx, "무엇을 판단하라는 지시"),
        MyOut,
        stage="column",
    )

    # skill 자체 검증: 프로파일 사실과 모순되면 되돌린다.
    # notes가 다음 시도의 repair_hints가 되므로 "무엇이 틀렸고 어떻게 고칠지" 함께 쓴다.
    f = column_fact(ctx, ctx.target)
    if out.label == "a" and f.get("distinct_ratio", 0) < 0.5:
        return SkillResult(
            skill="my-skill",
            ok=False,
            notes=f"label=a로 판정했으나 distinct_ratio={f.get('distinct_ratio')}로 근거가 없다. "
                  "b 또는 unknown을 사용하라.",
        )

    return semantic_result(
        "my-skill",
        ctx,
        {"kind": "my", "meaning": out.meaning, "label": out.label},
        out.confidence,
    )
