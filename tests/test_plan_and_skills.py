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
    LEAN_STAGES,
    REPLAN_STAGES,
    REQUIRED_PROMPTS,
    REQUIRED_SKILLS,
    STAGE_ORDER,
    first_pass_stages,
    flagged_columns,
    revision_steps,
    sanitize_gap_actions,
    sanitize_plan,
)


def test_sanitize_plan_drops_unknown_stages_and_duplicates():
    plan = sanitize_plan(
        {
            "steps": [
                {"stage": "table_context"},
                {"stage": "relation_analysis"},
                {"stage": "relation_analysis"},
                {"stage": "make_coffee"},
                {"stage": "explain_sparsity"},  # 보완 skill은 재계획 대상이 아니다
                "not a dict",
            ]
        }
    )
    assert [s["stage"] for s in plan["steps"]] == ["relation_analysis", "table_context"]


def test_revision_always_ends_in_validation_and_drops_table_context():
    steps = revision_steps({"steps": [{"stage": "table_context", "focus": []}]})
    assert [s["stage"] for s in steps] == ["semantic_validation"]


def test_revision_keeps_declared_validation_once():
    steps = revision_steps(
        {"steps": [{"stage": "semantic_validation", "focus": []}, {"stage": "relation_analysis", "focus": []}]}
    )
    assert [s["stage"] for s in steps] == ["relation_analysis", "semantic_validation"]


def test_domain_gap_gate_decides_whether_the_planner_runs():
    """게이트는 필드 하나를 보는 규칙이다. 임계값도, 두 번째 판정 호출도 없다."""
    interpretations = {
        "a": {"status": "resolved", "domain_gap": {"missing": "어느 공정인지"}},
        "b": {"status": "resolved", "domain_gap": None},
    }
    assert flagged_columns(interpretations) == ["a"]
    assert flagged_columns({"b": interpretations["b"]}) == []


def test_empty_gap_is_not_a_gap():
    """프롬프트는 '없으면 null'이라고 하지만 모델은 빈 껍데기를 낸다. 그걸 gap으로
    세면 아무것도 모른다는 보고 없이 컬럼이 planner로 넘어간다."""
    assert flagged_columns({"a": {"domain_gap": {}}}) == []
    assert flagged_columns({"a": {"domain_gap": ""}}) == []
    assert flagged_columns({"a": "해석이 dict가 아님"}) == []
    assert flagged_columns({"a": {}}) == []


def test_gate_can_be_narrowed_to_columns_that_changed():
    """2회차 이후에는 지난 라운드에 실제로 바뀐 컬럼만 다시 본다 - 손대지 않은
    컬럼은 같은 값이라 판정도 같다."""
    interpretations = {
        "a": {"domain_gap": {"missing": "x"}},
        "b": {"domain_gap": {"missing": "y"}},
    }
    assert flagged_columns(interpretations) == ["a", "b"]
    assert flagged_columns(interpretations, candidates=["b"]) == ["b"]
    assert flagged_columns(interpretations, candidates=[]) == []


def test_actions_are_filtered_to_what_can_actually_run():
    kept, dropped = sanitize_gap_actions(
        [
            {"action": "explain_sparsity", "columns": ["a"]},
            {"action": "joint_interpretation", "columns": ["a", "b"]},
            {"action": "explain_sparsity", "columns": ["ghost"]},
            {"action": "semantic_type", "columns": ["a"]},          # 보완 skill이 아니다
            {"action": "explain_sparsity", "columns": ["b"]},        # 게이트가 안 넘긴 컬럼
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


def test_groups_may_include_a_column_with_no_gap():
    """넘어온 컬럼이 하나라도 있으면 gap 없는 컬럼을 문맥으로 끼워도 된다."""
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
    assert first_pass_stages(True) == ["column_interpretation", "relation_analysis"]
    assert first_pass_stages(False) == ["column_interpretation"]


def test_each_folder_has_every_prompt_it_must_have():
    """프롬프트 추가 = <폴더>/<name>.md 파일 추가. 등록 절차가 없으니, 파일이
    빠졌다는 사실은 여기서만 드러난다."""
    for directory, required in [(PROMPT_DIR, REQUIRED_PROMPTS), (SKILL_DIR, REQUIRED_SKILLS)]:
        library = FileSystemPrompts(directory, required=required)
        for name in required:
            assert library.prompt(name).strip()


def test_missing_prompt_file_fails_loudly(tmp_path):
    with pytest.raises(RuntimeError) as e:
        FileSystemPrompts(tmp_path, required=["column_interpretation"])
    assert "column_interpretation" in str(e.value)


def test_the_two_folders_do_not_overlap():
    """폴더가 곧 '언제 도는가'다. 같은 이름이 양쪽에 있으면 그 답이 흐려진다."""
    stages = set(FileSystemPrompts(PROMPT_DIR).names())
    skills = set(FileSystemPrompts(SKILL_DIR).names())
    assert not stages & skills
    assert skills == set(GAP_SKILLS)
    assert set(STAGE_ORDER) <= stages


def test_gap_skills_are_never_planned_as_stages():
    """보완 skill은 재계획 스텝이 아니다 - 게이트를 거쳐 배정될 때만 붙는다."""
    assert not set(GAP_SKILLS) & set(STAGE_ORDER)


def test_replan_can_pick_any_fixed_stage():
    """고정 단계는 전부 되돌아갈 수 있는 지점이다."""
    assert REPLAN_STAGES == STAGE_ORDER
    plan = sanitize_plan({"steps": [{"stage": "make_coffee"}, {"stage": "relation_analysis"}]})
    assert [s["stage"] for s in plan["steps"]] == ["relation_analysis"]


def test_every_stage_has_a_lean_twin():
    """'모든 단계에 하나씩'이 계약이다 - 단계를 추가하고 최소 출력을 빠뜨리면
    그 단계만 비교 대상이 없어지고, 그건 파일이 없어질 때까지 티가 안 난다."""
    assert set(LEAN_STAGES) == set(STAGE_ORDER)
    assert set(LEAN_STAGES.values()) <= set(REQUIRED_PROMPTS)
    # 최소 출력 프롬프트가 고정 단계 이름을 덮어쓰지 않아야 한다.
    assert not set(LEAN_STAGES.values()) & set(STAGE_ORDER)


def test_lean_prompts_exist_as_files():
    library = FileSystemPrompts(PROMPT_DIR, required=list(LEAN_STAGES.values()))
    for name in LEAN_STAGES.values():
        assert library.prompt(name).strip()
