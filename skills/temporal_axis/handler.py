"""
temporal-axis handler.

시간 단위 주장을 handler의 if문이 아니라 TIME_COMPONENT probe로 검증한다.
probe는 실제 시:분:초 성분을 조사하므로, 표본에 ':'가 있는지만 보는
문자열 검사보다 정확하다. (표본 5개엔 없지만 전체엔 있는 경우를 놓치지 않는다)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import SkillContext, SkillDeps, SkillResult, VerifiableClaim
from agent.probes import ProbeKind, ProbeRequest
from agent.skill_utils import messages, semantic_result

Resolution = Literal["Year", "Quarter", "Month", "Week", "Day", "Hour", "Minute", "Second"]


class TemporalOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str = Field(description="이 시점이 무엇의 시점인지 1~2문장")
    resolution: Resolution
    role: str = Field(default="", description="event/recorded/valid_from 등. 불확실하면 빈 문자열")
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    finest = deps.probe(
        ctx.data_ref,
        ProbeRequest(kind=ProbeKind.TIME_COMPONENT, columns=[ctx.target], params={"resolution": "Day"}),
    )

    out: TemporalOut = await deps.structured(
        messages(
            ctx,
            "이 컬럼이 기록하는 시점의 의미와 시간 단위를 판단하라.",
            extra=f"[데이터 실측]\n- 실제로 구분 가능한 최소 단위: {finest.observed}",
        ),
        TemporalOut,
        stage="column",
    )

    result = semantic_result(
        "temporal-axis",
        ctx,
        {"kind": "temporal", "meaning": out.meaning, "resolution": out.resolution, "role": out.role},
        out.confidence,
    )
    result.claims.append(
        VerifiableClaim(
            statement=f"{ctx.target}는 {out.resolution} 시간 단위를 가진다",
            probe=ProbeRequest(
                kind=ProbeKind.TIME_COMPONENT,
                columns=[ctx.target],
                params={"resolution": out.resolution},
            ),
        )
    )
    return result
