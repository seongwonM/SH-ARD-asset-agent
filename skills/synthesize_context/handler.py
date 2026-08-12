"""
synthesize-context handler. 여러 슬롯을 한 번에 채우는 최종 skill.

검증 결과를 반드시 반영한다. 반증된 컬럼 의미는 프롬프트에서 제외하고,
반증 내역을 명시적으로 금지 목록으로 넣는다. 합성 단계가 이미 거짓으로
밝혀진 내용을 다시 끌어올리면 앞선 검증 전체가 무의미해진다.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot


class ContextOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(description="2~3 단어")
    summary: str = Field(description="3~4문장")
    key_points: List[str] = Field(default_factory=list, max_length=6)
    use_cases: List[str] = Field(default_factory=list, max_length=6)
    search_terms: List[str] = Field(default_factory=list, max_length=25)
    confidence: float = Field(ge=0.0, le=1.0)


def _refuted_columns(verification: Dict) -> set:
    """반증 리포트에서 대상 컬럼명을 추출한다."""
    out = set()
    for entry in verification.get("refuted", []):
        for token in entry.get("statement", "").replace("[", " ").replace("]", " ").split():
            cleaned = token.strip("',의는가을를에")
            if cleaned:
                out.add(cleaned)
    return out


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    values = ctx.board.get("values", {})
    keyed = ctx.board.get("keyed", {})
    profile = values.get(Slot.TABLE_PROFILE.value, {})
    semantics = keyed.get(Slot.COLUMN_SEMANTICS.value, {})
    grain = values.get(Slot.GRAIN.value, {})
    verification = values.get(Slot.VERIFICATION.value, {})
    constraints = values.get(Slot.CONSTRAINTS.value, {})
    compliance = values.get(Slot.COMPLIANCE.value, {})
    glossary = values.get(Slot.GLOSSARY.value, {})

    refuted_cols = _refuted_columns(verification)
    usable = {n: v for n, v in semantics.items() if n not in refuted_cols}

    col_lines = "\n".join(f"- {n}: {v.get('meaning','')}" for n, v in usable.items())
    coverage = f"{len(usable)}/{profile.get('column_count', 0)} 컬럼 해석됨"
    lang = "한국어로 작성한다." if ctx.language == "ko" else "Write in English."

    forbidden = ""
    if verification.get("refuted"):
        items = "\n".join(f"- {e['statement']}: {e['detail']}" for e in verification["refuted"])
        forbidden = f"\n[검증에서 반증된 내용 — 사용 금지]\n{items}\n"

    user = (
        f"[테이블]\n{ctx.asset_name}\n{ctx.source_description or '설명 없음'}\n"
        f"행 수: {profile.get('row_count', 0)}\n\n"
        f"[행 입도]\n{grain.get('grain', '미확정')}\n"
        f"{forbidden}\n"
        f"[컬럼 의미] ({coverage})\n{col_lines}\n\n"
        "위 정보만 사용해 이 자산의 주제, 요약, 검색어를 생성하라."
    )
    if ctx.repair_hints:
        user += "\n\n[직전 문제]\n" + "\n".join(f"- {h}" for h in ctx.repair_hints)

    msgs = [
        {"role": "system", "content": f"{ctx.instructions}\n\n유효한 JSON object 하나만 출력한다.\n{lang}"},
        {"role": "user", "content": user},
    ]
    out: ContextOut = await deps.structured(msgs, ContextOut, stage="expand")

    asset_context = {
        "asset_id": ctx.asset_id,
        "asset_name": ctx.asset_name,
        "topic": out.topic,
        "summary": out.summary,
        "key_points": out.key_points,
        "use_cases": out.use_cases,
        "grain": grain.get("grain", ""),
        "columns": [{"name": n, **v} for n, v in usable.items()],
        "linkage": list(keyed.get(Slot.LINKAGE.value, {}).values()),
        "coverage": coverage,
        # 신규 슬롯은 요약 생성에 쓰지 않고 그대로 실어 나른다.
        # LLM이 PII 판정이나 종속성을 문장으로 바꾸면 근거가 희석된다.
        "constraints": constraints,
        "compliance": compliance,
        "glossary": glossary,
        "verification": {
            "status": verification.get("status", "unknown"),
            "verified": len(verification.get("verified", [])),
            "refuted": [e["statement"] for e in verification.get("refuted", [])],
            "unverified_count": len(verification.get("unverified", [])),
            "contradictions": verification.get("contradictions", []),
            "probe_coverage": verification.get("coverage", 0.0),
        },
        # search_text는 LLM이 아니라 코드로 조립한다.
        # 다운스트림 검색이 이 문자열을 임베딩하므로 포맷이 흔들리면 안 된다.
        "search_text": _render_search_text(out, grain),
    }

    return SkillResult(
        skill="synthesize-context",
        contributions=[
            Contribution(slot=Slot.TOPIC, value=out.topic, confidence=out.confidence),
            Contribution(slot=Slot.SUMMARY, value=out.summary, confidence=out.confidence),
            Contribution(slot=Slot.SEARCH_TERMS, value=out.search_terms, confidence=out.confidence),
            Contribution(slot=Slot.ASSET_CONTEXT, value=asset_context, confidence=out.confidence),
        ],
        notes=coverage,
    )


def _render_search_text(out: ContextOut, grain: dict) -> str:
    parts = [f"Overview: {out.summary}"]
    if grain.get("grain"):
        parts.append(f"Grain: {grain['grain']}")
    if out.key_points:
        parts.append("Key Points: " + ", ".join(out.key_points))
    if out.use_cases:
        parts.append("Use Cases: " + ", ".join(out.use_cases))
    if out.search_terms:
        parts.append("Terms: " + ", ".join(out.search_terms))
    return "\n\n".join(parts)
