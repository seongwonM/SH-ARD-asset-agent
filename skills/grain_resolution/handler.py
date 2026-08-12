"""
grain-resolution handler.

행 단위 주장은 복합 컬럼 유일성으로 반증한다.
"한 행은 run_id 단위다"라는 주장은 run_id가 실제로 유일해야 성립한다.
LLM이 그럴듯한 키 조합을 말해도 데이터가 아니라면 통과하지 못한다.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot, VerifiableClaim
from agent.probes import ProbeKind, ProbeRequest


class GrainOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grain: str = Field(description="한 행이 무엇을 의미하는지 한 문장")
    key_columns: List[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    semantics = ctx.board.get("keyed", {}).get(Slot.COLUMN_SEMANTICS.value, {})
    linkage = ctx.board.get("keyed", {}).get(Slot.LINKAGE.value, {})

    lines = [
        f"- {name}: [{v.get('kind','')}] {v.get('meaning','')}"
        + (f" (role={v['role']})" if v.get("role") else "")
        for name, v in semantics.items()
    ]
    uniq_lines = [
        f"- {n}: 유일성 {v.get('uniqueness')}" for n, v in linkage.items() if v.get("uniqueness") is not None
    ]
    lang = "한국어로 작성한다." if ctx.language == "ko" else "Write in English."

    msgs = [
        {"role": "system", "content": f"{ctx.instructions}\n\n유효한 JSON object 하나만 출력한다.\n{lang}"},
        {
            "role": "user",
            "content": f"[테이블]\n{ctx.asset_name}\n{ctx.source_description or '설명 없음'}\n\n"
            f"[컬럼 의미]\n" + "\n".join(lines) + "\n\n"
            f"[식별자 유일성 실측]\n" + ("\n".join(uniq_lines) or "없음") + "\n\n"
            "이 테이블의 한 행이 무엇을 의미하는지, 즉 기록 단위를 판정하라."
            + ("\n\n[직전 문제]\n" + "\n".join(ctx.repair_hints) if ctx.repair_hints else ""),
        },
    ]
    out: GrainOut = await deps.structured(msgs, GrainOut, stage="table")

    known = set(semantics.keys())
    unknown = [c for c in out.key_columns if c not in known]
    if unknown:
        return SkillResult(
            skill="grain-resolution",
            ok=False,
            notes=f"존재하지 않는 컬럼을 키로 지목: {unknown}. 실제 컬럼 {sorted(known)} 만 사용하라.",
        )

    result = SkillResult(
        skill="grain-resolution",
        contributions=[
            Contribution(
                slot=Slot.GRAIN,
                value={"grain": out.grain, "key_columns": out.key_columns},
                evidence=[f"컬럼 의미 {len(semantics)}건 종합"],
                confidence=out.confidence,
            )
        ],
    )
    if out.key_columns:
        result.claims.append(
            VerifiableClaim(
                statement=f"{out.key_columns} 조합이 한 행을 유일하게 식별한다",
                probe=ProbeRequest(
                    kind=ProbeKind.UNIQUENESS, columns=out.key_columns, params={"min_ratio": 0.99}
                ),
                critical=True,
            )
        )
    return result
