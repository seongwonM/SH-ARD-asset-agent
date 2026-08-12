"""
identifier-link handler.

probe 사용의 대표 사례.
- 프롬프트를 만들기 전에 uniqueness probe로 사실을 먼저 확인해 LLM에 준다
- LLM이 role을 주장하면 그 주장을 반증할 probe를 claim으로 첨부한다

핸들러가 직접 if문으로 검사하지 않는다는 점이 중요하다.
검사를 claim으로 선언하면 executor가 실행하고, 실패 시 probe의 실측값이
그대로 재시도 힌트가 된다. 핸들러 안의 if문은 그 힌트를 직접 써야 한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot, VerifiableClaim
from agent.probes import ProbeKind, ProbeRequest
from agent.skill_utils import column_fact, messages, semantic_result

Role = Literal["primary", "reference", "business"]

# role별 유일성 기준. 이 값이 프롬프트와 probe 양쪽에 동시에 쓰인다.
ROLE_MIN_UNIQUENESS = {"primary": 0.99, "business": 0.0, "reference": 0.0}


class IdentifierOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str = Field(description="이 식별자가 무엇을 가리키는지 1~2문장")
    role: Role
    refers_to: str = Field(default="", description="컬럼명·설명에 근거가 있을 때만. 테이블명 추측 금지")
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    # 추측시키지 말고 먼저 확인해서 준다.
    uniq = deps.probe(
        ctx.data_ref,
        ProbeRequest(kind=ProbeKind.UNIQUENESS, columns=[ctx.target], params={"min_ratio": 0.99}),
    )
    nulls = deps.probe(
        ctx.data_ref, ProbeRequest(kind=ProbeKind.NULL_RATE, columns=[ctx.target], params={"max_rate": 0.0})
    )

    out: IdentifierOut = await deps.structured(
        messages(
            ctx,
            "이 식별자가 무엇을 가리키고 어떤 역할인지 판단하라.",
            extra=f"[데이터 실측]\n- 유일성 비율: {uniq.observed}\n- 결측 비율: {nulls.observed}",
        ),
        IdentifierOut,
        stage="column",
    )

    f = column_fact(ctx, ctx.target)
    linkage = Contribution(
        slot=Slot.LINKAGE,
        key=ctx.target,
        value={
            "column": ctx.target,
            "role": out.role,
            "refers_to": out.refers_to,
            "uniqueness": uniq.observed,
            "null_rate": nulls.observed,
        },
        evidence=[uniq.detail],
        confidence=out.confidence,
    )

    result = semantic_result(
        "identifier-link",
        ctx,
        {"kind": "identifier", "meaning": out.meaning, "role": out.role, "refers_to": out.refers_to},
        out.confidence,
        extra_slots=[linkage],
    )

    # role 주장을 데이터로 반증할 수 있게 한다.
    result.claims.append(
        VerifiableClaim(
            statement=f"{ctx.target}는 {out.role} 역할의 식별자다",
            probe=ProbeRequest(
                kind=ProbeKind.UNIQUENESS,
                columns=[ctx.target],
                params={"min_ratio": ROLE_MIN_UNIQUENESS[out.role]},
            ),
            critical=out.role == "primary",  # primary만 반증 시 실패 처리
        )
    )
    return result
