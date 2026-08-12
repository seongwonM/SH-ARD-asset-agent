"""value-pattern-profile handler. 문자열 형식 패턴을 결정론적으로 요약한다."""

from __future__ import annotations

import re

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^(01[0-9]|0[2-9][0-9]?)-?\d{3,4}-?\d{4}$")
DATE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")


def _classify(value: str) -> str:
    if EMAIL_RE.match(value):
        return "email_like"
    if PHONE_RE.match(value):
        return "phone_like"
    if DATE_RE.match(value):
        return "date_like"
    if value.startswith("{") and value.endswith("}"):
        return "json_like"
    if "/" in value:
        return "path_like"
    if value.isdigit():
        return "numeric_text"
    if value.isalpha():
        return "alpha_text"
    if any(c.isalpha() for c in value) and any(c.isdigit() for c in value):
        return "alphanumeric_code"
    return "other_text"


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    df = deps.dataframe(ctx.data_ref)
    contributions = []

    for col in profile.get("columns", []):
        name = col["name"]
        kind = col.get("kind", "")
        if kind not in {"identifier", "categorical", "free_text", "unknown"}:
            continue

        series = df[name].dropna().astype(str)
        if not len(series):
            continue

        labels = [_classify(v) for v in series.head(200).tolist()]
        counts = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        dominant = max(counts.items(), key=lambda x: x[1])[0]
        widths = sorted({len(v) for v in series.head(200).tolist()})
        value = {
            "dominant_pattern": dominant,
            "pattern_counts": counts,
            "fixed_width": len(widths) == 1,
            "widths": widths[:6],
            "examples": series.head(3).tolist(),
        }
        contributions.append(
            Contribution(
                slot=Slot.VALUE_PATTERNS,
                key=name,
                value=value,
                evidence=["string pattern scan"],
                confidence=1.0,
            )
        )

    return SkillResult(
        skill="value-pattern-profile",
        contributions=contributions,
        notes=f"{len(contributions)}개 컬럼 값 패턴 요약",
    )
