"""
dependency-check handler. LLM 미사용.

FUNCTIONAL_DEP probe를 후보 쌍에 전수 적용한다.
LLM에게 "어떤 종속이 있을 것 같냐"고 묻지 않는 이유:
그건 세면 되는 사실이고, 세는 편이 싸고 정확하다.
"""

from __future__ import annotations

from itertools import permutations

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot
from agent.probes import ProbeKind, ProbeRequest, run_probes

# 조합 폭발 방지. n개 컬럼이면 후보는 n*(n-1)개다.
MAX_PAIRS = 60
ELIGIBLE_KINDS = {"identifier", "categorical", "spatial"}


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    columns = [c for c in profile.get("columns", []) if c.get("kind") in ELIGIBLE_KINDS]
    names = [c["name"] for c in columns]

    pairs = [(a, b) for a, b in permutations(names, 2)]
    truncated = len(pairs) > MAX_PAIRS
    pairs = pairs[:MAX_PAIRS]

    holds, checked = [], 0
    if pairs:
        df = deps.dataframe(ctx.data_ref)
        results = run_probes(
            [ProbeRequest(kind=ProbeKind.FUNCTIONAL_DEP, columns=[a, b]) for a, b in pairs], df
        )
        for (a, b), r in zip(pairs, results):
            if r.error:
                continue
            checked += 1
            if r.passed:
                holds.append({"determinant": a, "dependent": b, "detail": r.detail})

    value = {
        "functional_dependencies": holds,
        "pairs_checked": checked,
        "pairs_skipped": truncated,
        # 검사하지 않은 것을 성립한다고 말하지 않기 위해 범위를 명시한다.
        "scope": f"{len(names)}개 식별자·범주형 컬럼 중 {checked}쌍 검사"
        + (f" (상한 {MAX_PAIRS}쌍으로 잘림)" if truncated else ""),
    }

    return SkillResult(
        skill="dependency-check",
        contributions=[
            Contribution(slot=Slot.CONSTRAINTS, value=value, confidence=1.0, evidence=["전수 probe"])
        ],
        notes=f"함수 종속 {len(holds)}건 확인 ({checked}쌍 검사)",
    )
