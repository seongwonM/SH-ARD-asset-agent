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
    REQUIRED_PROMPTS,
    REQUIRED_SKILLS,
    STAGE_ORDER,
    first_pass_stages,
    revision_steps,
    sanitize_gap_assignments,
    sanitize_plan,
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


def test_gap_assignments_are_filtered_to_real_columns_and_skills():
    kept = sanitize_gap_assignments(
        [
            {"column": "a", "skill": "explain_sparsity"},
            {"column": "ghost", "skill": "explain_sparsity"},
            {"column": "a", "skill": "semantic_type"},  # gap skill이 아니다
            "garbage",
        ],
        valid_columns=["a", "b"],
    )
    assert kept == [{"column": "a", "skill": "explain_sparsity"}]


def test_gap_assignments_tolerate_missing_field():
    assert sanitize_gap_assignments(None, ["a"]) == []


def test_relation_analysis_only_when_pairwise_evidence_exists():
    assert first_pass_stages(True) == ["semantic_type", "column_interpretation", "relation_analysis"]
    assert first_pass_stages(False) == ["semantic_type", "column_interpretation"]


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
    """보완 skill은 재계획 스텝이 아니다 - 컬럼별 보충으로만 붙는다."""
    assert not set(GAP_SKILLS) & set(STAGE_ORDER)
