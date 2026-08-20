"""무엇이 언제 도는지에 대한 규칙.

실행 단위는 두 종류이고, **누가 실행을 결정하는가**로 갈린다.

    고정 단계(prompts/)  코드가 정한 순서대로 돈다. 컬럼별 병렬 해석도, 테이블
                         맥락 생성도 여기다 - 무조건 만들어야 하는 산출물이라
                         "돌릴지 말지"를 물어볼 일이 없다. relation_analysis만
                         데이터 조건(pairwise 증거 유무)으로 켜고 끈다.
    보완 skill(skills/)  gap_planner가 그 컬럼에 필요하다고 판단할 때만 붙는다.
                         컬럼마다 부족한 점이 다르니 규칙표로 만들 수 없는 지점이고,
                         그래서 여기만 LLM 판단에 맡긴다.

LLM이 계획을 세우는 지점은 딱 두 군데다.

    gap_planner : 1차 해석 직후, 컬럼별로 무엇이 부족한지 판단해 보완 skill 배정
    planner     : 검증이 모순을 찾았을 때 어떤 고정 단계를 다시 돌릴지 재계획

두 곳 모두 LLM 출력을 그대로 믿지 않고 여기서 정제한다(모르는 이름, 없는 컬럼,
중복 스텝 제거). 정제 규칙이 프롬프트가 아니라 코드에 있는 이유는 프롬프트가
바뀌어도 실행 계약이 흔들리면 안 되기 때문이다.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

# 고정 단계의 정규 순서. 재계획도 이 순서를 벗어나지 못한다.
STAGE_ORDER = [
    "semantic_type",
    "column_interpretation",
    "relation_analysis",
    "semantic_validation",
    "table_context",
]

# 1차 해석 직후 컬럼별로 부족한 점이 있으면 gap_planner가 이 중에서 골라 붙인다.
# (Sato식 2단계 구조: 1차는 컬럼 독립적으로, 여기서는 다른 컬럼들의 확정 결과까지
# 보고 재조정)
GAP_SKILLS = ["reconsider_ambiguous", "explain_sparsity", "reconcile_type_meaning"]

# prompts/ 폴더에 반드시 있어야 하는 것: 고정 단계 + 계획 프롬프트 둘.
REQUIRED_PROMPTS = ["planner", "gap_planner", *STAGE_ORDER]
# skills/ 폴더에 반드시 있어야 하는 것: 보완 skill 전부.
REQUIRED_SKILLS = list(GAP_SKILLS)


def first_pass_stages(has_pairwise_evidence: bool) -> List[str]:
    """1차 고정 순서. relation_analysis만 데이터 근거로 조건부 포함한다 -
    pairwise 증거가 하나도 없으면 볼 게 없어서 호출 자체가 낭비다."""
    stages = ["semantic_type", "column_interpretation"]
    if has_pairwise_evidence:
        stages.append("relation_analysis")
    return stages


def sanitize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """replan 출력에서 실행 가능한 스텝만 남기고 정규 순서로 정렬한다.

    재계획이 고를 수 있는 것은 고정 단계뿐이다. 보완 skill은 컬럼별 배정이라
    planner가 아니라 gap_planner의 몫이고, 여기서 이름이 나와도 버린다.
    """
    raw_steps = plan.get("steps", [])
    valid_steps: List[Dict[str, Any]] = []
    seen = set()

    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        stage = step.get("stage")
        if stage in STAGE_ORDER and stage not in seen:
            valid_steps.append(
                {
                    "stage": stage,
                    "goal": step.get("goal", ""),
                    "focus": step.get("focus", []),
                }
            )
            seen.add(stage)

    valid_steps.sort(key=lambda x: STAGE_ORDER.index(x["stage"]))
    plan["steps"] = valid_steps
    return plan


def sanitize_gap_assignments(
    assignments: Any, valid_columns: Iterable[str]
) -> List[Dict[str, Any]]:
    """gap_planner 출력에서 실제 컬럼 + 실제 보완 skill 조합만 남긴다."""
    columns = set(valid_columns)
    if not isinstance(assignments, list):
        return []
    return [
        a
        for a in assignments
        if isinstance(a, dict) and a.get("column") in columns and a.get("skill") in GAP_SKILLS
    ]


def revision_steps(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """재계획 스텝을 실행 가능한 형태로 만든다.

    - table_context는 계획에서 뺀다. 재계획이 끝난 뒤 항상 새로 생성하기 때문에
      계획에 들어 있으면 중복 실행이 된다.
    - semantic_validation은 없으면 붙인다. 수정은 반드시 검증으로 끝나야 한다.
    """
    steps = [s for s in plan.get("steps", []) if s["stage"] != "table_context"]
    if "semantic_validation" not in [s["stage"] for s in steps]:
        steps.append(
            {
                "stage": "semantic_validation",
                "goal": "validate revised interpretation",
                "focus": [],
            }
        )
    steps.sort(key=lambda x: STAGE_ORDER.index(x["stage"]))
    return steps
