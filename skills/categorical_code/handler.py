"""
categorical-code handler.

두 방향을 모두 검사한다.
- VALUE_IN_SET     : 설명한 값이 실제로 존재하는가 (환각 차단, critical)
- SET_COVERS_COLUMN: 실제 값 중 설명 안 된 것이 있는가 (누락 경고, non-critical)

후자를 critical로 두면 카테고리가 많은 컬럼에서 계속 실패한다.
환각은 막고 누락은 기록만 하는 비대칭이 의도적이다.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import SkillContext, SkillDeps, SkillResult, VerifiableClaim
from agent.probes import ProbeKind, ProbeRequest
from agent.skill_utils import messages, semantic_result


class CategoryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str = Field(description="이 컬럼이 어떤 구분을 나타내는지")
    observed_values: List[str] = Field(
        default_factory=list, max_length=15, description="표본에 등장한 값만 '값: 의미' 형태로"
    )
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    out: CategoryOut = await deps.structured(
        messages(ctx, "이 컬럼이 어떤 분류 체계를 나타내는지 판단하라."),
        CategoryOut,
        stage="column",
    )

    labels = [item.split(":")[0].strip() for item in out.observed_values if item.strip()]
    result = semantic_result(
        "categorical-code",
        ctx,
        {"kind": "categorical", "meaning": out.meaning, "observed_values": out.observed_values},
        out.confidence,
    )
    if labels:
        result.claims.append(
            VerifiableClaim(
                statement=f"{ctx.target}에 값 {labels}가 존재한다",
                probe=ProbeRequest(
                    kind=ProbeKind.VALUE_IN_SET, columns=[ctx.target], params={"values": labels}
                ),
                critical=True,
            )
        )
        result.claims.append(
            VerifiableClaim(
                statement=f"{ctx.target}의 값을 모두 설명했다",
                probe=ProbeRequest(
                    kind=ProbeKind.SET_COVERS_COLUMN, columns=[ctx.target], params={"values": labels}
                ),
                critical=False,
            )
        )
    return result
