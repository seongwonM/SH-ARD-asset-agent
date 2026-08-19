"""core 계층은 LLM 없이 단독으로 검증한다. 여기 테스트에 어댑터가 등장하면
의존 방향이 깨진 것이다."""

from __future__ import annotations

import pandas as pd

from column_semantics.core.evidence import build_table_evidence
from column_semantics.core.naming import split_tokens
from column_semantics.core.profiling import datetime_profile, numeric_profile, profile_columns
from column_semantics.core.relations import build_relation_groups, find_grain_candidates


def test_split_tokens_handles_mixed_naming():
    assert split_tokens("equipmentID") == ["equipment", "id"]
    assert split_tokens("power_value_2") == ["power", "value", "2"]
    assert split_tokens("run-at.time") == ["run", "at", "time"]


def test_numeric_column_is_not_parsed_as_datetime():
    """pd.to_datetime(501)이 성공해버려서 정수 컬럼이 시간축으로 분류된 적이 있다.
    수치 dtype은 datetime 판정에서 아예 제외해야 한다."""
    s = pd.Series([501, 502, 503, 504])
    assert datetime_profile(s) is None
    assert numeric_profile(s) is not None


def test_numeric_profile_refuses_mostly_unparsable_column():
    s = pd.Series(["a", "b", "c", "1"])
    assert numeric_profile(s) is None


def test_profile_columns_reports_uniqueness_and_nulls(equipment_df):
    profiles = profile_columns(equipment_df)

    assert profiles["run_id"]["unique_ratio_non_null"] == 1.0
    assert profiles["equipment_id"]["unique_ratio_non_null"] < 1.0
    assert profiles["status_code"]["small_integer_domain"] == [0, 1]
    # 표본 값은 컬럼당 12개로 고정 - 행이 늘어도 payload가 커지지 않는다.
    assert len(profiles["run_id"]["sample_values"]) <= 12


def test_grain_candidate_finds_unique_column(equipment_df):
    grains = find_grain_candidates(equipment_df)
    single = [g for g in grains if g["columns"] == ["run_id"]]
    assert single and single[0]["unique_ratio"] == 1.0


def test_build_table_evidence_is_json_ready(equipment_df):
    import json

    evidence = build_table_evidence(equipment_df)
    assert set(evidence) == {"table", "column_profiles", "relation_evidence", "grain_candidates"}
    assert evidence["table"]["row_count"] == 12
    # numpy 스칼라가 남아 있으면 여기서 터진다.
    json.dumps(evidence, ensure_ascii=False)


def test_relation_groups_fall_back_to_pairwise_when_llm_did_not_run():
    groups, ungrouped = build_relation_groups(
        ["a", "b", "c"],
        relation_analysis_result=None,
        grain_candidates=[],
        pairwise_evidence=[{"columns": ["a", "b"], "pearson_corr": 0.9}],
    )
    assert sorted(groups[0]) == ["a", "b"]
    assert ungrouped == ["c"]


def test_relation_groups_prefer_llm_result_over_raw_pairwise():
    groups, ungrouped = build_relation_groups(
        ["a", "b", "c"],
        relation_analysis_result={"groups": [{"columns": ["b", "c"]}], "relations": []},
        grain_candidates=[],
        pairwise_evidence=[{"columns": ["a", "b"], "pearson_corr": 0.9}],
    )
    assert sorted(groups[0]) == ["b", "c"]
    assert ungrouped == ["a"]
