"""
기존 `TableAssetContextBuilder` 계약 호환성 검증.

run_robustness_test.py / analyze_robustness_test.py를 한 줄도 고치지 않고
쓰려면 출력 JSON의 **키 구조**가 정확히 같아야 한다.
이 테스트가 통과하지 않으면 이미 쌓인 robustness 결과와 신규 결과를 비교할 수 없다.
"""

from __future__ import annotations

import pandas as pd

from agent.compat import TableAssetContextBuilder
from fixtures import DF, SOURCE_DESC, MockDeps

# 기존 graph.py docstring이 명시한 반환 키
LEGACY_TOP_KEYS = {
    "input",
    "tabular_profile_output",
    "column_context_output",
    "initial_asset_context_output",
    "asset_context",
    "issues",
    "performance",
}
LEGACY_INPUT_KEYS = {
    "tabular_data",
    "asset_name",
    "source_description",
    "data_sample",
    "column_descriptions",
}
LEGACY_DETAIL_FIELDS = [
    "summary",
    "key_points",
    "use_cases",
    "related_concepts",
    "keywords",
    "additional_context",
]
LEGACY_PERF_KEYS = {
    "elapsed_seconds",
    "llm_call_count",
    "llm_avg_latency_seconds",
    "llm_prompt_tokens",
    "llm_completion_tokens",
    "llm_total_tokens",
    "qps",
    "tps",
}


def _build(**kw):
    builder = TableAssetContextBuilder(deps=MockDeps())
    return builder.build(
        tabular_data=DF,
        asset_name="process_log",
        source_description=SOURCE_DESC,
        **kw,
    )


def test_top_level_keys_preserved():
    r = _build()
    assert LEGACY_TOP_KEYS.issubset(r.keys()), f"누락된 옛 키: {LEGACY_TOP_KEYS - set(r)}"


def test_input_block_preserved():
    r = _build(column_descriptions={"power_value": "RF Generator 실제 출력값, 단위는 W"})
    assert LEGACY_INPUT_KEYS.issubset(r["input"].keys())
    assert r["input"]["asset_name"] == "process_log"
    assert r["input"]["column_descriptions"]["power_value"].startswith("RF")
    assert r["input"]["tabular_data"] == "pandas.DataFrame<process_log>"


def test_asset_context_details_six_fields():
    """SFD의 6개 섹션이 그대로 나와야 analyze 스크립트의 keywords 집계가 산다."""
    r = _build()
    details = r["asset_context"]["asset_context_details"]
    for f in LEGACY_DETAIL_FIELDS:
        assert f in details, f"asset_context_details.{f} 누락"
    assert isinstance(details["keywords"], list)
    assert isinstance(r["asset_context"]["search_text"], str)
    assert r["asset_context"]["search_text"]


def test_profile_and_column_context_shape():
    r = _build()
    assert set(r["tabular_profile_output"]) >= {"profile_summary", "coverage_summary"}
    assert "**power_value**" in r["tabular_profile_output"]["profile_summary"]
    assert "column_context_summary" in r["column_context_output"]
    assert "summary" in r["initial_asset_context_output"]


def test_performance_keys_preserved():
    r = _build()
    assert LEGACY_PERF_KEYS.issubset(r["performance"].keys())
    assert r["performance"]["llm_call_count"] > 0


def test_issues_shape():
    """issues 항목은 stage/error_type/message 3키를 유지해야 한다."""
    r = _build()
    for issue in r["issues"]:
        assert set(issue.keys()) == {"stage", "error_type", "message"}


def test_column_descriptions_reach_prompt():
    """
    기존 설명이 프롬프트에 실려야 with/without 비교(robustness의 핵심 축)가 의미를 갖는다.
    설명이 무시되면 두 조건의 결과가 같아져 실험 자체가 무효가 된다.
    """
    seen = {}

    class SpyDeps(MockDeps):
        async def structured(self, messages, model_cls, stage):
            seen.setdefault("text", []).append(messages[-1]["content"])
            return await super().structured(messages, model_cls, stage)

    builder = TableAssetContextBuilder(deps=SpyDeps())
    builder.build(
        tabular_data=DF,
        asset_name="process_log",
        source_description=SOURCE_DESC,
        column_descriptions={"power_value": "RF Generator 실제 출력값, 단위는 W"},
    )
    joined = "\n".join(seen["text"])
    assert "RF Generator" in joined, "column_descriptions가 프롬프트에 실리지 않음"


def test_new_fields_are_additive_only():
    """신규 필드는 추가만 하고 옛 키를 덮어쓰지 않는다."""
    r = _build()
    assert "trace" in r and isinstance(r["trace"], list)
    assert "verification" in r["asset_context"]
    # 옛 키가 신규 필드로 대체되지 않았는지
    assert "asset_context_details" in r["asset_context"]


def test_failure_still_returns_legacy_shape():
    """일부 실패해도 계약은 유지된다(배치 한 회차가 죽어도 JSONL 한 줄은 남아야 함)."""

    class StubbornDeps(MockDeps):
        async def structured(self, messages, model_cls, stage):
            if model_cls.__name__ == "MeasureOut":
                return model_cls(
                    meaning="출력값", unit="kPa", unit_evidence="column_name",
                    usage="모니터링", confidence=0.9,
                )
            return await super().structured(messages, model_cls, stage)

    builder = TableAssetContextBuilder(deps=StubbornDeps())
    r = builder.build(tabular_data=DF, asset_name="process_log", source_description=SOURCE_DESC)
    assert LEGACY_TOP_KEYS.issubset(r.keys())
    assert r["issues"], "실패가 issues에 기록되지 않음"
    assert r["asset_context"] is not None, "부분 실패에도 산출물은 나와야 한다"
