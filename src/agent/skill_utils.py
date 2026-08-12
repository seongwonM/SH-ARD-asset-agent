"""
per_column skill들이 공유하는 헬퍼.

각 skill의 handler.py가 짧게 유지되도록, 프롬프트 조립과
Contribution 변환처럼 반복되는 부분만 모았다.
스키마와 판단 규칙은 각 skill이 직접 정의한다 — 그게 skill의 정체성이므로.
"""

from __future__ import annotations

from typing import Dict, List

from agent.contract import Contribution, SkillContext, SkillResult, Slot

BASE_RULES = """규칙:
- 입력에서 확인되지 않는 단위, 기준값, 규격, 업무 규칙은 생성하지 않는다.
- 표본값에 없는 값을 예시로 들지 않는다.
- 다른 테이블과의 관계나 인과 관계를 주장하지 않는다.
- 불확실한 항목은 빈 문자열로 둔다.
- 유효한 JSON object 하나만 출력한다."""


def column_fact(ctx: SkillContext, name: str) -> Dict:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    for c in profile.get("columns", []):
        if c["name"] == name:
            return c
    return {}


def all_facts(ctx: SkillContext) -> List[Dict]:
    return ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {}).get("columns", [])


def column_block(ctx: SkillContext) -> str:
    f = column_fact(ctx, ctx.target)
    rng = f"{f.get('min_value','')} ~ {f.get('max_value','')}".strip(" ~") or "없음"
    return f"""[테이블]
- 이름: {ctx.asset_name}
- 설명: {ctx.source_description or '제공되지 않음'}

[대상 컬럼]
- 컬럼명: {f.get('name','')}
- 분류: {f.get('kind','')}
- dtype: {f.get('dtype','')}
- 고유값: {f.get('distinct_count',0)}개 (비율 {f.get('distinct_ratio',0)})
- 값 범위: {rng}
- 표본값: {f.get('samples',[])}"""


def messages(ctx: SkillContext, task: str, extra: str = "") -> List[Dict[str, str]]:
    """SKILL.md 본문을 system 프롬프트로 주입한다 (progressive disclosure의 실제 사용 지점)."""
    lang = "한국어로 작성한다." if ctx.language == "ko" else "Write in English."
    system = f"{ctx.instructions}\n\n{BASE_RULES}\n{lang}"
    user = column_block(ctx) + (f"\n\n{extra}" if extra else "") + f"\n\n{task}"
    if ctx.repair_hints:
        user += "\n\n[직전 출력의 문제 — 반드시 수정]\n" + "\n".join(f"- {h}" for h in ctx.repair_hints)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def semantic_result(skill: str, ctx: SkillContext, payload: Dict, confidence: float, extra_slots=None) -> SkillResult:
    contribs = [
        Contribution(
            slot=Slot.COLUMN_SEMANTICS,
            key=ctx.target,
            value=payload,
            evidence=[f"표본값 {column_fact(ctx, ctx.target).get('samples', [])}"],
            confidence=confidence,
        )
    ]
    contribs.extend(extra_slots or [])
    return SkillResult(skill=skill, contributions=contribs)
