"""계획 정제 규칙과 skill 폴더 계약.

LLM 출력을 그대로 실행하지 않는다는 것이 여기 테스트의 요지다 - 프롬프트가
바뀌어도 실행 계약은 코드가 지킨다.
"""

from __future__ import annotations

import pytest
from conftest import SKILL_DIR

from column_semantics.adapters.skills import FileSystemSkillLibrary
from column_semantics.pipeline.plan import (
    GAP_SKILLS,
    REQUIRED_SKILLS,
    SKILL_ORDER,
    first_pass_skills,
    revision_steps,
    sanitize_gap_assignments,
    sanitize_plan,
)


def test_sanitize_plan_drops_unknown_skills_and_duplicates():
    plan = sanitize_plan(
        {
            "steps": [
                {"skill": "table_context"},
                {"skill": "semantic_type"},
                {"skill": "semantic_type"},
                {"skill": "make_coffee"},
                "not a dict",
            ]
        }
    )
    assert [s["skill"] for s in plan["steps"]] == ["semantic_type", "table_context"]


def test_revision_always_ends_in_validation_and_drops_table_context():
    steps = revision_steps({"steps": [{"skill": "table_context", "focus": []}]})
    assert [s["skill"] for s in steps] == ["semantic_validation"]


def test_revision_keeps_declared_validation_once():
    steps = revision_steps(
        {"steps": [{"skill": "semantic_validation", "focus": []}, {"skill": "semantic_type", "focus": []}]}
    )
    assert [s["skill"] for s in steps] == ["semantic_type", "semantic_validation"]


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
    assert first_pass_skills(True) == ["semantic_type", "column_interpretation", "relation_analysis"]
    assert first_pass_skills(False) == ["semantic_type", "column_interpretation"]


def test_skill_folder_has_every_required_prompt():
    """skill 추가 = skills/<name>.md 파일 추가. 등록 절차가 없으니, 파일이
    빠졌다는 사실은 여기서만 드러난다."""
    library = FileSystemSkillLibrary(SKILL_DIR, required=REQUIRED_SKILLS)
    for name in REQUIRED_SKILLS:
        assert library.prompt(name).strip()


def test_missing_skill_file_fails_loudly(tmp_path):
    with pytest.raises(RuntimeError) as e:
        FileSystemSkillLibrary(tmp_path, required=["semantic_type"])
    assert "semantic_type" in str(e.value)


def test_gap_skills_are_not_in_the_main_order():
    """gap skill은 계획 스텝이 아니다 - 컬럼별 보충으로만 붙는다."""
    assert not set(GAP_SKILLS) & set(SKILL_ORDER)
