"""join-key-analysis handler. 조인 후보 키 강도를 결정론적으로 평가한다."""

from __future__ import annotations

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot


def _role_candidate(kind: str, distinct_ratio: float, null_ratio: float) -> tuple[str, list[str]]:
    notes = []
    if null_ratio > 0.2:
        notes.append("결측 비율이 높아 안정적인 조인 키로 쓰기 전에 확인이 필요함")
    if distinct_ratio >= 0.98 and null_ratio <= 0.05:
        return "entity_key", notes
    if distinct_ratio >= 0.2:
        return "reference_key", notes
    if kind == "categorical":
        return "grouping_code", notes
    return "weak_candidate", notes


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    contributions = []

    for col in profile.get("columns", []):
        kind = col.get("kind", "")
        if kind not in {"identifier", "categorical", "unknown"}:
            continue

        name = col["name"]
        distinct_ratio = float(col.get("distinct_ratio", 0.0) or 0.0)
        null_ratio = float(col.get("null_ratio", 0.0) or 0.0)
        role, notes = _role_candidate(kind, distinct_ratio, null_ratio)
        if distinct_ratio >= 0.98 and kind != "identifier":
            notes.append("이름은 식별자 형태가 아니지만 값은 거의 모두 다름")
        if distinct_ratio < 0.05:
            notes.append("값 종류가 적어 조인 키보다 그룹핑 코드에 가까움")

        contributions.append(
            Contribution(
                slot=Slot.JOIN_CANDIDATES,
                key=name,
                value={
                    "role_candidate": role,
                    "distinct_ratio": round(distinct_ratio, 4),
                    "null_ratio": round(null_ratio, 4),
                    "notes": notes,
                },
                evidence=["table_profile uniqueness/null stats"],
                confidence=1.0,
            )
        )

    return SkillResult(
        skill="join-key-analysis",
        contributions=contributions,
        notes=f"{len(contributions)}개 컬럼 조인 후보 평가",
    )
