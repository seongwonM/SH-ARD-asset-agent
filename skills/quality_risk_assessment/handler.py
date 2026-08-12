"""quality-risk-assessment handler. LLM 없이 프로파일 통계로 품질 위험을 요약한다."""

from __future__ import annotations

from agent.contract import Contribution, SkillContext, SkillDeps, SkillResult, Slot

HIGH_NULL = 0.3
VERY_HIGH_NULL = 0.7
HIGH_CARDINALITY = 0.9
CATEGORICAL_CARDINALITY = 30


async def run(ctx: SkillContext, deps: SkillDeps) -> SkillResult:
    profile = ctx.board.get("values", {}).get(Slot.TABLE_PROFILE.value, {})
    columns = profile.get("columns", [])
    row_count = int(profile.get("row_count", 0))

    high_null_columns = []
    unstable_join_candidates = []
    high_cardinality_categories = []

    for col in columns:
        name = col.get("name", "")
        kind = col.get("kind", "")
        null_ratio = float(col.get("null_ratio", 0.0) or 0.0)
        distinct_ratio = float(col.get("distinct_ratio", 0.0) or 0.0)
        distinct_count = int(col.get("distinct_count", 0) or 0)

        if null_ratio >= HIGH_NULL:
            severity = "high" if null_ratio >= VERY_HIGH_NULL else "medium"
            high_null_columns.append(
                {
                    "column": name,
                    "null_ratio": round(null_ratio, 4),
                    "severity": severity,
                }
            )

        if kind in {"identifier", "free_text", "unknown"} and distinct_ratio >= HIGH_CARDINALITY:
            unstable_join_candidates.append(
                {
                    "column": name,
                    "distinct_ratio": round(distinct_ratio, 4),
                    "reason": "값이 거의 모두 달라 조인 키나 그룹 기준으로 쓰기 전에 확인이 필요함",
                }
            )

        if kind == "categorical" and distinct_count > CATEGORICAL_CARDINALITY:
            high_cardinality_categories.append(
                {
                    "column": name,
                    "distinct_count": distinct_count,
                    "reason": "범주형으로 보기엔 값 종류가 많아 코드 체계 drift 가능성이 있음",
                }
            )

    duplicate_risk = {
        "row_count": row_count,
        "risk": "unknown",
        "reason": "고유 행 수를 직접 세지는 않음",
    }
    if row_count:
        duplicate_risk["risk"] = "review_needed"
        duplicate_risk["reason"] = "행 식별 키가 확정되기 전까지는 중복 여부를 단정할 수 없음"

    risk_count = len(high_null_columns) + len(unstable_join_candidates) + len(high_cardinality_categories)
    summary_parts = []
    if high_null_columns:
        summary_parts.append(f"결측 위험 {len(high_null_columns)}개 컬럼")
    if unstable_join_candidates:
        summary_parts.append(f"고카디널리티 위험 {len(unstable_join_candidates)}개 컬럼")
    if high_cardinality_categories:
        summary_parts.append(f"범주 drift 위험 {len(high_cardinality_categories)}개 컬럼")
    if not summary_parts:
        summary_parts.append("프로파일 통계 기준 뚜렷한 품질 위험 없음")

    value = {
        "summary": ", ".join(summary_parts),
        "high_null_columns": high_null_columns,
        "unstable_join_candidates": unstable_join_candidates,
        "high_cardinality_categories": high_cardinality_categories,
        "duplicate_risk": duplicate_risk,
        "risk_count": risk_count,
    }

    return SkillResult(
        skill="quality-risk-assessment",
        contributions=[
            Contribution(
                slot=Slot.QUALITY_RISKS,
                value=value,
                confidence=1.0,
                evidence=["table_profile 통계"],
            )
        ],
        notes=value["summary"],
    )
