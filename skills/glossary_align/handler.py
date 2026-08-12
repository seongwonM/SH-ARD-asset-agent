"""
glossary-align handler.

기존 레포의 metadata.json은 컬럼마다 표준용어/표준용어_내용/설명/추가설명을
하나의 문자열로 이어붙여 column_descriptions로 넘긴다. 그 안에 표준용어가
이미 들어 있으므로, 여기서 할 일은 추출이지 생성이 아니다.

추출한 용어가 실제로 원문에 있는지 코드로 되짚어 확인한다.
LLM에게 "지어내지 마라"고 말하는 것만으로는 부족하다.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    term: str = Field(description="설명에 문자 그대로 등장한 표준용어만. 없으면 빈 문자열")


class GlossaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: List[Mapping] = Field(default_factory=list, max_length=60)


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    descriptions: Dict[str, str] = ctx.column_descriptions or {}

    # 설명이 없으면 LLM을 부르지 않는다. 빈 결과로 슬롯만 채워 루프를 진행시킨다.
    if not descriptions:
        return SkillResult(
            skill="glossary-align",
            contributions=[
                Contribution(
                    slot=Slot.GLOSSARY,
                    value={"mappings": {}, "unmapped": [], "note": "제공된 컬럼 설명이 없어 정렬 생략"},
                    confidence=1.0,
                )
            ],
            notes="column_descriptions 없음 - 생략",
        )

    listing = "\n".join(f"- {col}: {desc}" for col, desc in descriptions.items())
    lang = "한국어로 작성한다." if ctx.language == "ko" else "Write in English."
    user = (
        f"[컬럼별 기존 설명]\n{listing}\n\n"
        "각 컬럼의 설명에서 사내 표준용어에 해당하는 표현을 찾아 정렬하라. "
        "설명에 문자 그대로 등장하지 않는 용어는 절대 쓰지 말고 빈 문자열로 둔다."
    )
    if ctx.repair_hints:
        user += "\n\n[직전 문제]\n" + "\n".join(f"- {h}" for h in ctx.repair_hints)

    out: GlossaryOut = await deps.structured(
        [
            {"role": "system", "content": f"{ctx.instructions}\n\nJSON object 하나만 출력한다.\n{lang}"},
            {"role": "user", "content": user},
        ],
        GlossaryOut,
        stage="table",
    )

    # 창작 차단: 추출한 용어가 원문에 실제로 있는지 되짚는다.
    mappings, invented = {}, []
    for m in out.mappings:
        if not m.term:
            continue
        source = descriptions.get(m.column, "")
        if m.term in source:
            mappings[m.column] = m.term
        else:
            invented.append(f"{m.column}={m.term}")

    if invented:
        return SkillResult(
            skill="glossary-align",
            ok=False,
            notes=f"설명 원문에 없는 용어를 표준용어로 지목: {invented}. "
                  "원문에 문자 그대로 있는 표현만 쓰거나 빈 문자열로 두라.",
        )

    unmapped = [c for c in descriptions if c not in mappings]
    return SkillResult(
        skill="glossary-align",
        contributions=[
            Contribution(
                slot=Slot.GLOSSARY,
                value={"mappings": mappings, "unmapped": unmapped},
                confidence=1.0,
                evidence=["column_descriptions 원문 대조"],
            )
        ],
        notes=f"표준용어 {len(mappings)}건 정렬, 미정의 {len(unmapped)}건",
    )
