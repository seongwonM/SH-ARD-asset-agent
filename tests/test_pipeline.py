"""파이프라인 전 구간. LLM은 가짜고 데이터는 진짜다 - probe가 실제로 데이터를
보고 LLM 주장을 뒤집는지까지 여기서 확인한다."""

from __future__ import annotations

import json

import pytest
from fakes import FakeLLM

from column_semantics.adapters.skills import InMemorySkillLibrary
from column_semantics.core.evidence import build_table_evidence
from column_semantics.pipeline.orchestrator import PipelineConfig, run_pipeline
from column_semantics.pipeline.plan import REQUIRED_SKILLS
from column_semantics.pipeline.skill_runner import SkillRunner


def make_runner(llm: FakeLLM) -> SkillRunner:
    skills = InMemorySkillLibrary({name: f"# {name} 프롬프트" for name in REQUIRED_SKILLS})
    return SkillRunner(skills=skills, llm=llm)


@pytest.fixture
def run_result(equipment_df):
    llm = FakeLLM()
    result = run_pipeline(
        equipment_df,
        make_runner(llm),
        config=PipelineConfig(max_rounds=2, max_workers=4, source_name="equipment_log.csv"),
    )
    return llm, result


def test_result_shape_is_stable(run_result):
    """결과 JSON의 최상위 블록은 계약이다 - 다운스트림(로그 수집/분석)이 이걸 본다."""
    _, result = run_result
    assert set(result) == {"meta", "plans", "evidence", "results", "timeline"}
    assert result["meta"]["status"] == "done"
    assert result["evidence"]["table"]["source_file"] == "equipment_log.csv"
    json.dumps(result, ensure_ascii=False)


def test_first_pass_runs_in_fixed_order(run_result):
    llm, _ = run_result
    labels = llm.labels()
    assert labels[0] == "semantic_type"
    # 1차 해석은 컬럼별 병렬 호출이다.
    interp = [x for x in labels if x.startswith("column_interpretation:")]
    assert len(interp) >= 6
    assert labels.index("gap_planner") > labels.index("semantic_type")


def test_column_payload_is_scoped_to_that_column(run_result):
    """컬럼 단위 skill에 테이블 전체 프로파일을 넣지 않는다 - 컬럼이 늘수록
    무관한 정보가 판단을 흐리고 토큰만 커진다."""
    llm, _ = run_result
    payload = llm.payload_for("column_interpretation:power_value")
    assert payload["column_profile"]["name"] == "power_value"
    assert "column_profiles" not in payload


def test_bogus_gap_assignments_are_never_executed(run_result):
    llm, _ = run_result
    labels = llm.labels()
    assert "reconsider_ambiguous:power_value" in labels
    # 없는 컬럼 / 없는 skill 배정은 실행되지 않아야 한다.
    assert not any("없는컬럼" in x for x in labels)
    assert not any("존재하지_않는_skill" in x for x in labels)


def test_gap_skill_result_is_merged_into_the_column(equipment_df):
    """ambiguous로 남은 컬럼에 gap skill이 붙고, 그 출력이 해당 컬럼에 반영된다.
    (수정 라운드가 없는 경우 - 재해석이 돌면 그 컬럼은 통째로 새 결과로 덮인다.)"""
    llm = FakeLLM(refute_first_validation=False)
    result = run_pipeline(
        equipment_df, make_runner(llm), config=PipelineConfig(max_rounds=1, max_workers=4)
    )
    assert "replan" not in llm.labels()

    column = result["results"]["column_interpretation"]["columns"]["power_value"]
    assert column["status"] == "resolved"
    assert column["selected_meaning"] == "설비 출력값(W)"
    assert column["gap_history"][0]["skill"] == "reconsider_ambiguous"
    # 다른 컬럼은 gap 대상이 아니므로 손대지 않는다.
    assert "gap_history" not in result["results"]["column_interpretation"]["columns"]["run_id"]


def test_probe_refutes_llm_pass_and_triggers_revision(run_result):
    """이 파이프라인의 핵심 주장: 텍스트끼리 대조하는 게 아니라 데이터에 대고
    반증한다. LLM이 pass라고 써도 실측이 어긋나면 수정 라운드가 열려야 한다."""
    llm, result = run_result
    assert "replan" in llm.labels()
    assert result["plans"], "재계획이 기록되어야 한다"

    round2 = result["plans"][0]
    assert round2["round"] == 2
    # table_context는 계획에서 제거되고, semantic_validation은 자동으로 붙는다.
    steps = [s["skill"] for s in round2["steps"]]
    assert steps == ["column_interpretation", "semantic_validation"]

    # 재해석은 focus한 컬럼만 다시 돈다.
    reexec = [
        e
        for e in result["timeline"]
        if e.get("skill") == "column_interpretation" and e.get("phase") == "re-exec"
    ]
    assert len(reexec) == 1


def test_revision_ends_in_pass_and_regenerates_table_context(run_result):
    _, result = run_result
    assert result["meta"]["validation_status"] == "pass"
    contexts = [
        e for e in result["timeline"] if e.get("skill") == "table_context" and e.get("event") == "skill"
    ]
    # 1차 마무리 + 수정 후 재생성
    assert [c["phase"] for c in contexts] == ["exec", "re-exec"]


def test_timeline_records_every_llm_call(run_result):
    llm, result = run_result
    calls = [e for e in result["timeline"] if e.get("event") == "llm_call"]
    # 가짜 LLM은 타임라인을 기록하지 않는다(그건 어댑터의 일이다).
    assert calls == []
    # 대신 skill 단위 구간은 오케스트레이터가 남긴다.
    skills = {e["skill"] for e in result["timeline"] if e.get("event") == "skill"}
    assert {"semantic_type", "column_interpretation", "semantic_validation", "table_context"} <= skills
    assert all("elapsed_seconds" in e for e in result["timeline"])


def test_checkpoint_is_written_as_each_skill_finishes(equipment_df):
    saved = []
    run_pipeline(
        equipment_df,
        make_runner(FakeLLM()),
        config=PipelineConfig(max_rounds=1, max_workers=4, on_checkpoint=lambda r: saved.append(r)),
    )
    assert len(saved) >= 4
    assert saved[0]["meta"]["status"] == "in_progress"
    # 체크포인트는 그 시점까지의 결과만 담는다 - 마지막 것이 가장 많은 skill을 담아야 한다.
    assert len(saved[-1]["results"]) >= len(saved[0]["results"])


def test_relation_analysis_is_skipped_without_pairwise_evidence():
    """pairwise 증거가 없으면 볼 게 없다 - 호출 자체를 하지 않는다."""
    import pandas as pd

    df = pd.DataFrame({"only_col": ["a", "b", "c", "d"]})
    evidence = build_table_evidence(df)
    assert evidence["relation_evidence"]["pairwise"] == []

    llm = FakeLLM(refute_first_validation=False)
    run_pipeline(df, make_runner(llm), config=PipelineConfig(max_rounds=1, max_workers=2))
    assert "relation_analysis" not in llm.labels()
