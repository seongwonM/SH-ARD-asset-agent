"""
verify-context handler.

교차 검증 + 검증률 집계. LLM 호출이 없다.

핵심 설계: 이 skill은 "전부 맞다"고 말하지 않는다.
검증된 주장과 검증 수단이 없는 주장을 구분해서 남기는 것이 목적이다.
unverified 목록이 곧 사람이 봐야 할 큐가 된다.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot
from agent.probes import ProbeKind, ProbeRequest, run_probes


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    keyed = ctx.board.get("keyed", {})
    values = ctx.board.get("values", {})
    semantics: Dict[str, Any] = keyed.get(Slot.COLUMN_SEMANTICS.value, {})
    linkage: Dict[str, Any] = keyed.get(Slot.LINKAGE.value, {})
    grain: Dict[str, Any] = values.get(Slot.GRAIN.value, {})

    checks: List[Dict[str, Any]] = []

    # 1. 식별자 role 재확인
    for col, info in linkage.items():
        role = info.get("role", "")
        checks.append(
            {
                "statement": f"{col}의 역할은 {role}이다",
                "probe": ProbeRequest(
                    kind=ProbeKind.UNIQUENESS,
                    columns=[col],
                    params={"min_ratio": 0.99 if role == "primary" else 0.0},
                ),
                "scope": "linkage",
            }
        )

    # 2. 입도 키 재확인
    keys = grain.get("key_columns") or []
    if keys:
        checks.append(
            {
                "statement": f"{keys} 조합이 행을 유일하게 식별한다",
                "probe": ProbeRequest(kind=ProbeKind.UNIQUENESS, columns=keys, params={"min_ratio": 0.99}),
                "scope": "grain",
            }
        )

    # 3. 교차 모순: reference로 판정된 컬럼이 단독 grain 키로 쓰였는가
    contradictions: List[str] = []
    if len(keys) == 1 and keys[0] in linkage:
        role = linkage[keys[0]].get("role")
        if role == "reference":
            contradictions.append(
                f"{keys[0]}는 reference(반복 참조)로 판정됐는데 단독 입도 키로 사용됐다. "
                "둘 중 하나가 틀렸다."
            )

    # 4. 범주형 값 재확인
    for col, info in semantics.items():
        vals = [v.split(":")[0].strip() for v in (info.get("observed_values") or [])]
        if vals:
            checks.append(
                {
                    "statement": f"{col}의 설명된 값이 실제로 존재한다",
                    "probe": ProbeRequest(
                        kind=ProbeKind.VALUE_IN_SET, columns=[col], params={"values": vals}
                    ),
                    "scope": "categorical",
                }
            )

    verified, refuted, unverified = [], [], []
    if checks:
        try:
            df = deps.dataframe(ctx.data_ref)
            outcomes = run_probes([c["probe"] for c in checks], df)
            for chk, res in zip(checks, outcomes):
                entry = {"statement": chk["statement"], "scope": chk["scope"], "detail": res.detail}
                if res.error:
                    entry["detail"] = res.error
                    unverified.append(entry)
                elif res.passed:
                    verified.append(entry)
                else:
                    refuted.append(entry)
        except Exception as exc:  # noqa: BLE001
            unverified = [{"statement": c["statement"], "scope": c["scope"], "detail": str(exc)} for c in checks]

    # 5. 검증 수단이 없는 주장 집계 — 자연어 meaning은 probe로 확인할 수 없다
    unverifiable_meanings = [
        {"statement": f"{col}: {info.get('meaning','')}", "scope": "meaning", "detail": "자연어 주장이라 데이터 검증 불가"}
        for col, info in semantics.items()
        if info.get("meaning")
    ]
    unverified.extend(unverifiable_meanings)

    total = len(verified) + len(refuted) + len(unverified)
    report = {
        "verified": verified,
        "refuted": refuted,
        "unverified": unverified,
        "contradictions": contradictions,
        "checked_count": len(verified) + len(refuted),
        "coverage": round((len(verified) + len(refuted)) / total, 3) if total else 0.0,
        "status": "refuted" if (refuted or contradictions) else "clean",
    }

    return SkillResult(
        skill="verify-context",
        contributions=[
            Contribution(slot=Slot.VERIFICATION, value=report, confidence=1.0, evidence=["probe 재실행"])
        ],
        notes=f"검증 {len(verified)}건 통과, {len(refuted)}건 반증, {len(unverified)}건 검증불가",
    )
