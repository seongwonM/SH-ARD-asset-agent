"""무엇이 언제 도는지에 대한 규칙.

실행 단위는 두 종류이고, **누가 실행을 결정하는가**로 갈린다.

    고정 단계(prompts/)  코드가 정한 순서대로 돈다. 컬럼별 병렬 해석도, 검토도,
                         테이블 맥락 생성도 여기다 - 무조건 만들어야 하는 산출물이라
                         "돌릴지 말지"를 물어볼 일이 없다. relation_analysis만
                         데이터 조건(pairwise 증거 유무)으로 켜고 끈다.
    보완 skill(skills/)  gap_planner가 배정할 때만 붙는다. 컬럼마다 부족한 점이
                         다르니 규칙표로 만들 수 없는 지점이고, 그래서 여기만
                         LLM 판단에 맡긴다.

보완이 붙기까지 판단이 두 번 갈라진다.

    column_review : 컬럼별로 "더 볼 필요가 있나"만. 무엇을 할지는 정하지 않는다 -
                    컬럼 하나만 보고서는 "이 둘을 같이 봐야 한다"를 알 수 없다.
    gap_planner   : 넘어온 컬럼들을 한자리에서 보고 무엇을 할지. 여러 컬럼을 묶는
                    판단이 여기서만 가능하다.
    planner       : 검증이 모순을 찾았을 때 어떤 고정 단계를 다시 돌릴지 재계획

모두 LLM 출력을 그대로 믿지 않고 여기서 정제한다(모르는 이름, 없는 컬럼,
중복 제거, 예산). 정제 규칙이 프롬프트가 아니라 코드에 있는 이유는 프롬프트가
바뀌어도 실행 계약이 흔들리면 안 되기 때문이다.

**정제는 판단하지 않는다.** 실행 가능한지(컬럼이 실재하는지, 아는 행동인지,
예산 안인지)만 본다. "이 근거가 충분한가"는 걸러내지 않는다 - 그걸 코드가
판정하려면 semantic_type마다 임계값을 정해야 하고, 그 임계값은 LLM이 이미
아는 상식을 실험으로 다시 알아내는 일이 된다. 근거(`cites`/`reason`)는
버리지 않고 계획 문서에 그대로 남겨, 나중에 실제 값과 대조할 수 있게 한다.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# 고정 단계의 정규 순서.
STAGE_ORDER = [
    "semantic_type",
    "column_interpretation",
    "column_review",
    "relation_analysis",
    "semantic_validation",
    "table_context",
]

# 재계획(planner)이 고를 수 있는 단계. column_review는 빠진다 - 검토는 보완
# 루프가 스스로 다시 도는 것이지, 검증 실패 때 되돌아갈 지점이 아니다.
REPLAN_STAGES = [s for s in STAGE_ORDER if s != "column_review"]

# 검토를 통과하지 못한 컬럼에 gap_planner가 이 중에서 골라 붙인다.
# (Sato식 2단계 구조: 1차는 컬럼 독립적으로, 여기서는 다른 컬럼들의 확정 결과까지
# 보고 재조정)
GAP_SKILLS = [
    "reconsider_ambiguous",
    "explain_sparsity",
    "reconcile_type_meaning",
    "joint_interpretation",
]

# 여러 컬럼을 한 번에 보는 보완. 나머지는 컬럼 하나짜리다.
MULTI_COLUMN_SKILLS = {"joint_interpretation"}

# 보완 루프 예산. 늘리기 전에 robustness로 효과부터 볼 것 - 라운드를 늘리면
# 토큰은 확실히 늘지만 해석이 나아진다는 보장은 없다.
MAX_GAP_ROUNDS = 2
MAX_ACTIONS_PER_COLUMN = 2
MAX_GROUP_COLUMNS = 4

# prompts/ 폴더에 반드시 있어야 하는 것: 고정 단계 + 계획 프롬프트 둘.
REQUIRED_PROMPTS = ["planner", "gap_planner", *STAGE_ORDER]
# skills/ 폴더에 반드시 있어야 하는 것: 보완 skill 전부.
REQUIRED_SKILLS = list(GAP_SKILLS)


def first_pass_stages(has_pairwise_evidence: bool) -> List[str]:
    """1차 고정 순서. relation_analysis만 데이터 근거로 조건부 포함한다 -
    pairwise 증거가 하나도 없으면 볼 게 없어서 호출 자체가 낭비다."""
    stages = ["semantic_type", "column_interpretation", "column_review"]
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
        if stage in REPLAN_STAGES and stage not in seen:
            valid_steps.append(
                {
                    "stage": stage,
                    "goal": step.get("goal", ""),
                    "focus": step.get("focus", []),
                }
            )
            seen.add(stage)

    valid_steps.sort(key=lambda x: REPLAN_STAGES.index(x["stage"]))
    plan["steps"] = valid_steps
    return plan


def flagged_columns(reviews: Dict[str, Any]) -> List[str]:
    """검토가 넘긴 컬럼. 이게 비면 gap_planner를 아예 부르지 않는다.

    planner가 도는 조건은 임계값이 아니라 검토 결과다 - 넘어온 게 없으면 계획할
    것도 없고, 근거 없이 부르는 호출이 된다.
    """
    return [
        col
        for col, review in reviews.items()
        if isinstance(review, dict) and review.get("verdict") == "needs_work"
    ]


def sanitize_review(review: Any) -> Dict[str, Any]:
    """검토 출력을 실행 가능한 형태로만 줄인다.

    verdict가 두 리터럴 중 하나가 아니면 pass로 본다 - 형식을 못 맞춘 응답을
    "더 보자"로 해석하면, 프롬프트가 깨졌을 때 모든 컬럼이 planner로 넘어간다.
    `gap`/`cites`는 내용을 검사하지 않고 그대로 싣는다.
    """
    if not isinstance(review, dict):
        return {"verdict": "pass", "gap": "", "cites": []}
    verdict = review.get("verdict")
    cites = review.get("cites")
    return {
        "verdict": verdict if verdict in {"pass", "needs_work"} else "pass",
        "gap": review.get("gap") or "",
        "cites": cites if isinstance(cites, list) else [],
    }


def sanitize_gap_actions(
    actions: Any,
    flagged: Iterable[str],
    all_columns: Iterable[str],
    action_counts: Optional[Dict[str, int]] = None,
    done: Optional[Set[Tuple[str, Tuple[str, ...]]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """gap_planner 출력에서 실행 가능한 행동만 남기고, 버린 것은 이유와 함께 돌려준다.

    거르는 것은 전부 "실행할 수 있는가"다 - 아는 행동인지, 컬럼이 실재하는지,
    검토가 넘긴 컬럼을 대상으로 하는지, 예산 안인지, 이미 한 일인지. 근거가
    충분한지는 보지 않는다.

    `action_counts`와 `done`은 호출을 넘어 누적된다. 라운드가 바뀌었다고 같은
    (행동, 컬럼집합)을 다시 돌리면 같은 입력에 같은 답이 나오고 호출만 는다.
    버린 행동도 계획 문서에 남는다: planner가 무엇을 하려 했는지가 사라지면
    프롬프트를 고칠 근거도 사라진다.
    """
    flagged_set = set(flagged)
    known = set(all_columns)
    counts = action_counts if action_counts is not None else {}
    seen: Set[Tuple[str, Tuple[str, ...]]] = done if done is not None else set()
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            dropped.append({"action": action, "why": "dict가 아님"})
            continue

        name = action.get("action")
        columns = [c for c in action.get("columns", []) if isinstance(c, str)]
        record = {**action, "columns": columns}

        if name not in GAP_SKILLS:
            dropped.append({**record, "why": f"모르는 행동: {name}"})
            continue
        unknown = [c for c in columns if c not in known]
        if unknown:
            dropped.append({**record, "why": f"없는 컬럼: {unknown}"})
            continue
        if not columns:
            dropped.append({**record, "why": "대상 컬럼 없음"})
            continue

        multi = name in MULTI_COLUMN_SKILLS
        if multi and len(columns) < 2:
            dropped.append({**record, "why": "여러 컬럼을 보는 행동인데 컬럼이 하나"})
            continue
        if not multi and len(columns) > 1:
            dropped.append({**record, "why": "컬럼 하나짜리 행동인데 여러 컬럼 지정"})
            continue
        if len(columns) > MAX_GROUP_COLUMNS:
            dropped.append({**record, "why": f"그룹이 너무 큼(>{MAX_GROUP_COLUMNS})"})
            continue
        # 그룹에는 검토를 통과한 컬럼이 문맥으로 낄 수 있다. 다만 넘어온 컬럼이
        # 하나도 없는 그룹은 아무도 요청하지 않은 작업이다.
        if not (set(columns) & flagged_set):
            dropped.append({**record, "why": "검토가 넘긴 컬럼이 대상에 없음"})
            continue

        key = (name, tuple(sorted(columns)))
        if key in seen:
            dropped.append({**record, "why": "같은 행동 중복(이미 실행했거나 같은 계획 안에서 반복)"})
            continue

        over = [c for c in columns if counts.get(c, 0) >= MAX_ACTIONS_PER_COLUMN]
        if over:
            dropped.append({**record, "why": f"컬럼 행동 예산 초과: {over}"})
            continue

        seen.add(key)
        for c in columns:
            counts[c] = counts.get(c, 0) + 1
        kept.append(record)

    return kept, dropped


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
    steps.sort(key=lambda x: REPLAN_STAGES.index(x["stage"]))
    return steps
