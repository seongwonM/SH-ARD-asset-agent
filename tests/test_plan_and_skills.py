"""계획 정제 규칙과 프롬프트 폴더 계약.

LLM 출력을 그대로 실행하지 않는다는 것이 여기 테스트의 요지다 - 프롬프트가
바뀌어도 실행 계약은 코드가 지킨다. 폴더가 둘로 갈린 것(고정 단계 / 보완 skill)
자체도 계약이라 같이 확인한다.
"""

from __future__ import annotations

import pytest
from conftest import PROMPT_DIR, SKILL_DIR

from column_semantics.adapters.prompts import FileSystemPrompts
from column_semantics.pipeline.plan import (
    GAP_SKILLS,
    REPLAN_STAGES,
    REQUIRED_PROMPTS,
    REQUIRED_SKILLS,
    STAGE_ORDER,
    first_pass_stages,
    flagged_columns,
    revision_steps,
    sanitize_gap_actions,
    sanitize_plan,
    sanitize_review,
)


def test_sanitize_plan_drops_unknown_stages_and_duplicates():
    plan = sanitize_plan(
        {
            "steps": [
                {"stage": "table_context"},
                {"stage": "semantic_type"},
                {"stage": "semantic_type"},
                {"stage": "make_coffee"},
                {"stage": "explain_sparsity"},  # 보완 skill은 재계획 대상이 아니다
                "not a dict",
            ]
        }
    )
    assert [s["stage"] for s in plan["steps"]] == ["semantic_type", "table_context"]


def test_revision_always_ends_in_validation_and_drops_table_context():
    steps = revision_steps({"steps": [{"stage": "table_context", "focus": []}]})
    assert [s["stage"] for s in steps] == ["semantic_validation"]


def test_revision_keeps_declared_validation_once():
    steps = revision_steps(
        {"steps": [{"stage": "semantic_validation", "focus": []}, {"stage": "semantic_type", "focus": []}]}
    )
    assert [s["stage"] for s in steps] == ["semantic_type", "semantic_validation"]


def test_review_gate_decides_whether_the_planner_runs():
    reviews = {
        "a": {"verdict": "needs_work", "gap": "..."},
        "b": {"verdict": "pass", "gap": ""},
    }
    assert flagged_columns(reviews) == ["a"]
    assert flagged_columns({"b": {"verdict": "pass"}}) == []


def test_malformed_review_counts_as_pass():
    """형식을 못 맞춘 응답을 '더 보자'로 읽으면 프롬프트가 깨졌을 때 전부 넘어간다."""
    assert sanitize_review({"verdict": "그런듯"})["verdict"] == "pass"
    assert sanitize_review(None)["verdict"] == "pass"
    assert sanitize_review({"verdict": "needs_work"})["verdict"] == "needs_work"


def test_review_reason_is_carried_through_untouched():
    """근거는 검증하지 않는다 - 기록해두고 나중에 실제 값과 대조할 수 있게만 한다."""
    review = sanitize_review(
        {"verdict": "needs_work", "gap": "빈 값이 많다", "cites": [{"field": "없는필드", "value": 1}]}
    )
    assert review["gap"] == "빈 값이 많다"
    assert review["cites"] == [{"field": "없는필드", "value": 1}]


def test_actions_are_filtered_to_what_can_actually_run():
    kept, dropped = sanitize_gap_actions(
        [
            {"action": "explain_sparsity", "columns": ["a"]},
            {"action": "joint_interpretation", "columns": ["a", "b"]},
            {"action": "explain_sparsity", "columns": ["ghost"]},
            {"action": "semantic_type", "columns": ["a"]},          # 보완 skill이 아니다
            {"action": "explain_sparsity", "columns": ["b"]},        # 검토가 안 넘긴 컬럼
            {"action": "joint_interpretation", "columns": ["a"]},    # 그룹인데 하나
            {"action": "explain_sparsity", "columns": ["a", "b"]},   # 단일인데 여럿
            "garbage",
        ],
        flagged=["a"],
        all_columns=["a", "b"],
    )
    assert [(k["action"], k["columns"]) for k in kept] == [
        ("explain_sparsity", ["a"]),
        ("joint_interpretation", ["a", "b"]),
    ]
    assert len(dropped) == 6
    assert all(d["why"] for d in dropped)


def test_groups_may_include_a_column_that_passed_review():
    """넘어온 컬럼이 하나라도 있으면 통과한 컬럼을 문맥으로 끼워도 된다."""
    kept, _ = sanitize_gap_actions(
        [{"action": "joint_interpretation", "columns": ["a", "settled"]}],
        flagged=["a"],
        all_columns=["a", "settled"],
    )
    assert kept and kept[0]["columns"] == ["a", "settled"]


def test_action_budget_is_per_column_and_carries_across_rounds():
    counts = {"a": 2}
    kept, dropped = sanitize_gap_actions(
        [{"action": "explain_sparsity", "columns": ["a"]}],
        flagged=["a"],
        all_columns=["a"],
        action_counts=counts,
    )
    assert kept == []
    assert "예산 초과" in dropped[0]["why"]


def test_duplicate_actions_are_collapsed():
    kept, dropped = sanitize_gap_actions(
        [
            {"action": "explain_sparsity", "columns": ["a"]},
            {"action": "explain_sparsity", "columns": ["a"]},
        ],
        flagged=["a"],
        all_columns=["a"],
    )
    assert len(kept) == 1
    assert "중복" in dropped[0]["why"]


def test_the_same_action_is_not_repeated_in_a_later_round():
    """라운드가 바뀌었다고 같은 일을 다시 하면 같은 답에 호출만 는다."""
    done = set()
    counts = {}
    action = [{"action": "explain_sparsity", "columns": ["a"]}]

    kept, _ = sanitize_gap_actions(action, ["a"], ["a"], counts, done)
    assert len(kept) == 1

    kept2, dropped2 = sanitize_gap_actions(action, ["a"], ["a"], counts, done)
    assert kept2 == []
    assert "중복" in dropped2[0]["why"]


def test_gap_actions_tolerate_missing_field():
    assert sanitize_gap_actions(None, ["a"], ["a"]) == ([], [])


def test_relation_analysis_only_when_pairwise_evidence_exists():
    assert first_pass_stages(True) == [
        "semantic_type",
        "column_interpretation",
        "column_review",
        "relation_analysis",
    ]
    assert first_pass_stages(False) == ["semantic_type", "column_interpretation", "column_review"]


def test_each_folder_has_every_prompt_it_must_have():
    """프롬프트 추가 = <폴더>/<name>.md 파일 추가. 등록 절차가 없으니, 파일이
    빠졌다는 사실은 여기서만 드러난다."""
    for directory, required in [(PROMPT_DIR, REQUIRED_PROMPTS), (SKILL_DIR, REQUIRED_SKILLS)]:
        library = FileSystemPrompts(directory, required=required)
        for name in required:
            assert library.prompt(name).strip()


def test_missing_prompt_file_fails_loudly(tmp_path):
    with pytest.raises(RuntimeError) as e:
        FileSystemPrompts(tmp_path, required=["semantic_type"])
    assert "semantic_type" in str(e.value)


def test_the_two_folders_do_not_overlap():
    """폴더가 곧 '언제 도는가'다. 같은 이름이 양쪽에 있으면 그 답이 흐려진다."""
    stages = set(FileSystemPrompts(PROMPT_DIR).names())
    skills = set(FileSystemPrompts(SKILL_DIR).names())
    assert not stages & skills
    assert skills == set(GAP_SKILLS)
    assert set(STAGE_ORDER) <= stages


def test_gap_skills_are_never_planned_as_stages():
    """보완 skill은 재계획 스텝이 아니다 - 검토를 거쳐 배정될 때만 붙는다."""
    assert not set(GAP_SKILLS) & set(STAGE_ORDER)


def test_replan_cannot_pick_the_review_stage():
    """검토는 보완 루프가 스스로 다시 도는 것이지, 검증 실패 때 되돌아갈 지점이 아니다."""
    assert "column_review" in STAGE_ORDER
    assert "column_review" not in REPLAN_STAGES
    plan = sanitize_plan({"steps": [{"stage": "column_review"}, {"stage": "semantic_type"}]})
    assert [s["stage"] for s in plan["steps"]] == ["semantic_type"]
