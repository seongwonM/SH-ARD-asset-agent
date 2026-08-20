"""파이프라인 전 구간. LLM은 가짜고 데이터는 진짜다 - probe가 실제로 데이터를
보고 LLM 주장을 뒤집는지까지 여기서 확인한다."""

from __future__ import annotations

import json

import pytest
from fakes import FakeLLM

from column_semantics.adapters.prompts import InMemoryPrompts
from column_semantics.core.evidence import build_table_evidence
from column_semantics.pipeline.documents import PARTS
from column_semantics.pipeline.orchestrator import PipelineConfig, run_pipeline
from column_semantics.pipeline.plan import REQUIRED_PROMPTS, REQUIRED_SKILLS
from column_semantics.pipeline.stage_runner import StageRunner


def make_runner(llm: FakeLLM) -> StageRunner:
    def library(names):
        return InMemoryPrompts({name: f"# {name} 프롬프트" for name in names})

    return StageRunner(
        stages=library(REQUIRED_PROMPTS), skills=library(REQUIRED_SKILLS), llm=llm
    )


@pytest.fixture
def run_result(equipment_df):
    llm = FakeLLM()
    docs = run_pipeline(
        equipment_df,
        make_runner(llm),
        config=PipelineConfig(max_rounds=2, max_workers=4, source_name="equipment_log.csv"),
    )
    return llm, docs


def test_document_set_is_stable(run_result):
    """문서 5벌과 각 문서의 최상위 블록은 계약이다 - 배치 로그 수집과 결과 분석이
    이 구조를 본다."""
    _, docs = run_result
    assert set(docs) == set(PARTS)
    assert set(docs["columns"]) == {"meta", "columns"}
    assert set(docs["rulebase"]) == {
        "meta",
        "table",
        "column_profiles",
        "relation_evidence",
        "grain_candidates",
        "probes",
    }
    assert set(docs["plan"]) == {"meta", "first_pass", "gap_planning", "replans", "execution"}
    assert set(docs["table"]) == {"meta", "table_context", "relation_analysis", "validation"}
    assert set(docs["llm_calls"]) == {"meta", "prompts", "calls"}

    for part, doc in docs.items():
        assert doc["meta"]["part"] == part
        assert doc["meta"]["status"] == "done"
        json.dumps(doc, ensure_ascii=False)

    assert docs["rulebase"]["table"]["source_file"] == "equipment_log.csv"


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


def test_every_call_carries_its_stage(run_result):
    """호출 기록이 어느 단계에서 나온 것인지 알 수 있어야 llm_calls 문서가 쓸모 있다."""
    llm, _ = run_result
    context = llm.context_for("column_interpretation:power_value")
    assert context["name"] == "column_interpretation"
    assert context["column"] == "power_value"
    assert context["phase"] == "exec"
    assert context["round"] == 1
    assert llm.context_for("replan")["stage"] == "replan"


def test_call_records_say_whether_it_was_a_fixed_stage_or_a_supplement(run_result):
    """호출 기록만 보고도 '코드가 돌린 것'과 '보완으로 붙은 것'이 갈려야 한다."""
    llm, _ = run_result
    kind = {c["label"]: c["context"]["kind"] for c in llm.calls}
    assert kind["semantic_type"] == "stage"
    assert kind["column_interpretation:power_value"] == "stage"
    assert kind["table_context"] == "stage"
    assert kind["reconsider_ambiguous:power_value"] == "skill"
    # 계획 호출은 산출물이 아니라 실행 결정이라 둘 중 어느 쪽도 아니다.
    assert kind["gap_planner"] == "planner"
    assert kind["replan"] == "planner"


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
    docs = run_pipeline(
        equipment_df, make_runner(llm), config=PipelineConfig(max_rounds=1, max_workers=4)
    )
    assert "replan" not in llm.labels()

    column = docs["columns"]["columns"]["power_value"]["final"]["interpretation"]
    assert column["status"] == "resolved"
    assert column["selected_meaning"] == "설비 출력값(W)"
    assert column["gap_history"][0]["skill"] == "reconsider_ambiguous"
    # 다른 컬럼은 gap 대상이 아니므로 손대지 않는다.
    other = docs["columns"]["columns"]["run_id"]["final"]["interpretation"]
    assert "gap_history" not in other


def test_column_stages_keep_what_each_step_changed(equipment_df):
    """이 문서의 존재 이유: 최종값만 남기면 'gap 보충이 무엇을 바꿨는지'가 사라진다."""
    llm = FakeLLM(refute_first_validation=False)
    docs = run_pipeline(
        equipment_df, make_runner(llm), config=PipelineConfig(max_rounds=1, max_workers=4)
    )
    stages = docs["columns"]["columns"]["power_value"]["stages"]
    kinds = [s["stage"] for s in stages]
    assert kinds[:2] == ["semantic_type", "column_interpretation"]
    assert "gap" in kinds

    # 1차 해석 단계는 gap이 덮어쓰기 전 값을 그대로 들고 있어야 한다.
    first = next(s for s in stages if s["stage"] == "column_interpretation")
    assert first["value"]["status"] == "ambiguous"
    assert "gap_history" not in first["value"]

    gap = next(s for s in stages if s["stage"] == "gap")
    assert gap["skill"] == "reconsider_ambiguous"
    # 어느 라운드/국면의 변화인지가 모든 단계에 붙어야 시간순으로 읽을 수 있다.
    assert all(s["round"] == 1 and s["phase"] == "exec" for s in stages)
    assert gap["before"]["status"] == "ambiguous"
    assert gap["after"]["selected_meaning"] == "설비 출력값(W)"
    assert set(gap["changed"]) >= {"status", "selected_meaning"}
    # 손대지 않은 컬럼은 gap 단계 자체가 없다.
    assert "gap" not in [s["stage"] for s in docs["columns"]["columns"]["run_id"]["stages"]]


def test_probe_refutes_llm_pass_and_triggers_revision(run_result):
    """이 파이프라인의 핵심 주장: 텍스트끼리 대조하는 게 아니라 데이터에 대고
    반증한다. LLM이 pass라고 써도 실측이 어긋나면 수정 라운드가 열려야 한다."""
    llm, docs = run_result
    assert "replan" in llm.labels()
    assert docs["plan"]["replans"], "재계획이 기록되어야 한다"

    round2 = docs["plan"]["replans"][0]
    assert round2["round"] == 2
    # table_context는 계획에서 제거되고, semantic_validation은 자동으로 붙는다.
    steps = [s["stage"] for s in round2["steps"]]
    assert steps == ["column_interpretation", "semantic_validation"]
    # LLM 원출력은 정제 전 그대로 남는다 - 코드가 무엇을 걸러냈는지 대조할 수 있어야 한다.
    assert [s["stage"] for s in round2["raw"]["steps"]] == [
        "column_interpretation",
        "table_context",
    ]

    # 재해석은 focus한 컬럼만 다시 돈다.
    reexec = [
        e
        for e in docs["plan"]["execution"]
        if e.get("stage") == "column_interpretation" and e.get("phase") == "re-exec"
    ]
    assert len(reexec) == 1


def test_measurements_live_only_in_the_rulebase_document(run_result):
    """룰베이스로 계산한 값은 한곳에만 둔다. 다른 문서는 id로만 가리킨다."""
    _, docs = run_result
    probes = docs["rulebase"]["probes"]
    assert probes, "probe가 실행됐어야 한다"
    measured = probes[0]
    assert measured["observed"]["true_ratio"] < 0.95
    assert measured["probe_id"] == "probe-1"

    checks = docs["table"]["validation"]["rounds"][0]["checks"]
    refuted = next(c for c in checks if c.get("probe_id") == "probe-1")
    assert refuted["status"] == "fail"  # LLM은 pass라고 썼지만 실측이 뒤집었다
    assert "observed" not in refuted or not isinstance(refuted.get("observed"), dict)

    # 컬럼 문서는 check를 id와 판정으로만 참조한다.
    stages = docs["columns"]["columns"]["power_value"]["stages"]
    validation = next(s for s in stages if s["stage"] == "semantic_validation")
    assert validation["value"]["checks"][0]["check_id"] == refuted["check_id"]


def test_failed_checks_carry_measurements_back_into_the_retry(run_result):
    """반증의 근거가 곧 재시도 힌트다 - 저장은 갈라놓되 피드백에는 실측값을 붙인다."""
    llm, _ = run_result
    payload = llm.payload_for("column_interpretation:power_value")  # 1차 호출
    assert payload["revision_feedback"] is None

    replan_payload = llm.payload_for("replan")
    failed = replan_payload["validation_feedback"]["checks"]
    assert failed and failed[0]["measured"]["true_ratio"] < 0.95


def test_revision_ends_in_pass_and_regenerates_table_context(run_result):
    _, docs = run_result
    assert docs["table"]["meta"]["validation_status"] == "pass"
    contexts = [
        e
        for e in docs["plan"]["execution"]
        if e.get("stage") == "table_context" and e.get("event") == "stage"
    ]
    # 1차 마무리 + 수정 후 재생성
    assert [c["phase"] for c in contexts] == ["exec", "re-exec"]
    # 검증은 라운드별로 전부 남는다.
    assert [r["round"] for r in docs["table"]["validation"]["rounds"]] == [1, 2]
    assert docs["table"]["validation"]["final_status"] == "pass"


def test_execution_trace_covers_every_skill(run_result):
    _, docs = run_result
    execution = docs["plan"]["execution"]
    stages = {e["stage"] for e in execution if e.get("event") == "stage"}
    assert {"semantic_type", "column_interpretation", "semantic_validation", "table_context"} <= stages
    assert all("elapsed_seconds" in e for e in execution)
    # 1차 고정 순서와 gap 배정 근거도 계획 문서 안에 있다.
    assert docs["plan"]["first_pass"]["stages"][0] == "semantic_type"
    assert docs["plan"]["gap_planning"]["assignments"][0]["column"] == "power_value"
    assert len(docs["plan"]["gap_planning"]["raw"]["gap_assignments"]) == 3  # 정제 전 원본


def test_checkpoint_is_written_as_each_skill_finishes(equipment_df):
    saved = []
    run_pipeline(
        equipment_df,
        make_runner(FakeLLM()),
        config=PipelineConfig(max_rounds=1, max_workers=4, on_checkpoint=lambda d: saved.append(d)),
    )
    assert len(saved) >= 4
    assert set(saved[0]) == set(PARTS)
    assert saved[0]["columns"]["meta"]["status"] == "in_progress"
    # 체크포인트는 그 시점까지의 결과만 담는다 - 뒤로 갈수록 단계가 쌓여야 한다.
    def stage_count(docs):
        return sum(len(c["stages"]) for c in docs["columns"]["columns"].values())

    assert stage_count(saved[-1]) >= stage_count(saved[0])


def test_relation_analysis_is_skipped_without_pairwise_evidence():
    """pairwise 증거가 없으면 볼 게 없다 - 호출 자체를 하지 않는다."""
    import pandas as pd

    df = pd.DataFrame({"only_col": ["a", "b", "c", "d"]})
    evidence = build_table_evidence(df)
    assert evidence["relation_evidence"]["pairwise"] == []

    llm = FakeLLM(refute_first_validation=False)
    docs = run_pipeline(df, make_runner(llm), config=PipelineConfig(max_rounds=1, max_workers=2))
    assert "relation_analysis" not in llm.labels()
    assert docs["plan"]["first_pass"]["relation_analysis_included"] is False
