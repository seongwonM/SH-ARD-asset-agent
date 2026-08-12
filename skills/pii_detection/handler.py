"""
pii-detection handler.

direct 판정은 정규식 probe로 반증한다. LLM이 컬럼명만 보고
"user_email이니까 이메일"이라고 하면, 표본값이 실제로 이메일 형태가 아닐 때
그 판정은 기각된다.

과탐/미탐 비대칭을 코드로도 반영한다: quasi는 probe를 걸지 않는다.
quasi를 엄격히 검사하면 애매한 컬럼이 none으로 떨어지는데,
그 방향의 실수가 더 비싸다.
"""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot, VerifiableClaim
from agent.probes import ProbeKind, ProbeRequest

Level = Literal["direct", "quasi", "none"]

# direct 판정별 반증 패턴. LLM이 고르게 하지 않고 코드가 소유한다.
# 패턴을 LLM이 만들면 자기 판정에 맞는 느슨한 패턴을 만들어 검증이 무력해진다.
PATTERNS = {
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "phone_kr": r"^0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}$",
    "rrn_kr": r"^\d{6}[-]?\d{7}$",
    "card": r"^\d{4}[-. ]?\d{4}[-. ]?\d{4}[-. ]?\d{4}$",
    "ip": r"^\d{1,3}(\.\d{1,3}){3}$",
}


class PiiColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    level: Level
    kind: str = Field(default="", description=f"direct면 {list(PATTERNS)} 중 하나. 아니면 빈 문자열")
    note: str = Field(default="", description="판단 근거 한 줄. 표본값을 그대로 옮기지 말 것")


class PiiOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: List[PiiColumn] = Field(default_factory=list, max_length=80)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    cols = profile.get("columns", [])
    listing = "\n".join(
        f"- {c['name']} ({c.get('kind','')}, dtype={c.get('dtype','')}): 표본 {c.get('samples', [])[:3]}"
        for c in cols
    )
    lang = "한국어로 작성한다." if ctx.language == "ko" else "Write in English."

    user = (
        f"[테이블]\n{ctx.asset_name}\n{ctx.source_description or '설명 없음'}\n\n"
        f"[컬럼]\n{listing}\n\n"
        f"각 컬럼의 개인정보 등급을 판정하라. direct면 kind를 {list(PATTERNS)} 중에서 고른다."
    )
    if ctx.repair_hints:
        user += "\n\n[직전 문제]\n" + "\n".join(f"- {h}" for h in ctx.repair_hints)

    out: PiiOut = await deps.structured(
        [
            {"role": "system", "content": f"{ctx.instructions}\n\nJSON object 하나만 출력한다.\n{lang}"},
            {"role": "user", "content": user},
        ],
        PiiOut,
        stage="table",
    )

    known = {c["name"] for c in cols}
    unknown = [c.column for c in out.columns if c.column not in known]
    if unknown:
        return SkillResult(
            skill="pii-detection", ok=False,
            notes=f"존재하지 않는 컬럼을 판정: {unknown}. 실제 컬럼 {sorted(known)} 만 사용하라.",
        )

    tagged = [c for c in out.columns if c.level != "none"]
    result = SkillResult(
        skill="pii-detection",
        contributions=[
            Contribution(
                slot=Slot.COMPLIANCE,
                value={
                    "pii_columns": [
                        {"column": c.column, "level": c.level, "kind": c.kind, "note": c.note}
                        for c in tagged
                    ],
                    "direct_count": sum(1 for c in tagged if c.level == "direct"),
                    "quasi_count": sum(1 for c in tagged if c.level == "quasi"),
                },
                confidence=0.9,
            )
        ],
        notes=f"PII 컬럼 {len(tagged)}건 (direct {sum(1 for c in tagged if c.level=='direct')}건)",
    )

    # direct 판정만 패턴으로 반증한다. quasi는 의도적으로 검사하지 않는다.
    for c in tagged:
        if c.level != "direct" or c.kind not in PATTERNS:
            continue
        result.claims.append(
            VerifiableClaim(
                statement=f"{c.column}은(는) {c.kind} 형태의 직접 식별정보다",
                probe=ProbeRequest(
                    kind=ProbeKind.REGEX_MATCH,
                    columns=[c.column],
                    params={"pattern": PATTERNS[c.kind], "min_ratio": 0.7},
                ),
                critical=True,
            )
        )
    return result
