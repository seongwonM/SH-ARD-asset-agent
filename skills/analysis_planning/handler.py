"""analysis-planning handler. evidence를 읽고 다음 분석 계획을 세운다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import AnalysisNeed, Artifact, Contribution, SkillContext, SkillDeps, SkillResult, SkillRole, Slot
from agent.skill_utils import artifact_lines


class PlanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    focus: str
    rationale: str
    ready_slots: list[str] = Field(default_factory=list)
    requested_slots: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    user = (
        f"[테이블]\n- 이름: {ctx.asset_name}\n- 설명: {ctx.source_description or '설명 없음'}\n"
        f"- 행 수: {profile.get('row_count', 0)}\n- 컬럼 수: {profile.get('column_count', 0)}\n\n"
        f"[현재 evidence artifacts]\n{artifact_lines(ctx)}\n\n"
        "지금 당장 진행 가능한 해석 슬롯과, 증거가 부족해서 추가로 더 봐야 할 슬롯을 구분하라."
    )
    if ctx.repair_hints:
        user += "\n\n[직전 문제]\n" + "\n".join(f"- {h}" for h in ctx.repair_hints)

    out: PlanOut = await deps.structured(
        [
            {
                "role": "system",
                "content": (
                    f"{ctx.instructions}\n\n"
                    "유효한 JSON object 하나만 출력한다. slot 이름은 기존 슬롯만 사용한다."
                ),
            },
            {"role": "user", "content": user},
        ],
        PlanOut,
        stage="plan",
    )

    requested = []
    needs = []
    for name in out.requested_slots:
        try:
            slot = Slot(name)
        except ValueError:
            continue
        requested.append(slot)
        needs.append(AnalysisNeed(slot=slot, reason=out.rationale, priority=1))

    plan = {
        "focus": out.focus,
        "rationale": out.rationale,
        "ready_slots": out.ready_slots,
        "requested_slots": [s.value for s in requested],
    }
    artifact = Artifact(
        artifact_type="analysis_plan",
        producer="analysis-planning",
        role=SkillRole.DELIBERATOR,
        slot=Slot.ANALYSIS_PLAN,
        scope={"asset_name": ctx.asset_name},
        payload=plan,
        evidence=["artifact summary", f"{len(ctx.board.get('artifacts', []))} artifacts reviewed"],
        confidence=out.confidence,
    )
    return SkillResult(
        skill="analysis-planning",
        contributions=[Contribution(slot=Slot.ANALYSIS_PLAN, value=plan, confidence=out.confidence)],
        artifacts=[artifact],
        requests=requested,
        analysis_needs=needs,
        notes=out.focus,
    )
