"""직접 runner 출력 구조 검증."""

from __future__ import annotations

from agent.runner import TableAssetContextRunner
from fixtures import DF, SOURCE_DESC, MockDeps

TOP_KEYS = {
    "input",
    "column_analysis",
    "data_interpretation",
    "asset_context",
    "issues",
    "performance",
    "trace",
}
INPUT_KEYS = {
    "tabular_data",
    "asset_name",
    "source_description",
    "column_descriptions",
}
DETAIL_FIELDS = [
    "summary",
    "key_points",
    "use_cases",
    "related_concepts",
    "keywords",
    "additional_context",
]
PERF_KEYS = {
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
    builder = TableAssetContextRunner(deps=MockDeps())
    return builder.build(
        tabular_data=DF,
        asset_name="process_log",
        source_description=SOURCE_DESC,
        **kw,
    )


def test_top_level_keys_preserved():
    r = _build()
    assert TOP_KEYS.issubset(r.keys()), f"누락된 키: {TOP_KEYS - set(r)}"


def test_input_block_preserved():
    r = _build(column_descriptions={"power_value": "RF Generator 실제 출력값, 단위는 W"})
    assert INPUT_KEYS.issubset(r["input"].keys())
    assert r["input"]["asset_name"] == "process_log"
    assert r["input"]["column_descriptions"]["power_value"].startswith("RF")
    assert r["input"]["tabular_data"] == "pandas.DataFrame<process_log>"


def test_asset_context_details_six_fields():
    r = _build()
    details = r["asset_context"]["asset_context_details"]
    for f in DETAIL_FIELDS:
        assert f in details, f"asset_context_details.{f} 누락"
    assert isinstance(details["keywords"], list)
    assert isinstance(r["asset_context"]["summary"], str)
    assert r["asset_context"]["summary"]


def test_column_and_data_blocks_present():
    r = _build()
    assert "summary" in r["column_analysis"]
    assert "columns" in r["column_analysis"]
    assert "grain" in r["data_interpretation"]
    assert "record_unit" in r["data_interpretation"]
    assert "quality_risks" in r["data_interpretation"]
    assert "verification" in r["data_interpretation"]
    names = {c["name"] for c in r["column_analysis"]["columns"]}
    assert names == set(DF.columns)


def test_performance_keys_preserved():
    r = _build()
    assert PERF_KEYS.issubset(r["performance"].keys())
    assert r["performance"]["llm_call_count"] > 0


def test_issues_shape():
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

    builder = TableAssetContextRunner(deps=SpyDeps())
    builder.build(
        tabular_data=DF,
        asset_name="process_log",
        source_description=SOURCE_DESC,
        column_descriptions={"power_value": "RF Generator 실제 출력값, 단위는 W"},
    )
    joined = "\n".join(seen["text"])
    assert "RF Generator" in joined, "column_descriptions가 프롬프트에 실리지 않음"


def test_new_fields_are_additive_only():
    r = _build()
    assert "trace" in r and isinstance(r["trace"], list)
    assert "verification" in r["asset_context"]
    assert "quality_risks" in r["asset_context"]
    assert "record_unit" in r["asset_context"]
    assert "asset_context_details" in r["asset_context"]
    assert "column_analysis" in r
    assert "data_interpretation" in r


def test_failure_still_returns_legacy_shape():
    """일부 실패해도 실행 결과 형태는 유지된다."""

    class StubbornDeps(MockDeps):
        async def structured(self, messages, model_cls, stage):
            if model_cls.__name__ == "MeasureOut":
                return model_cls(
                    meaning="출력값", unit="kPa", unit_evidence="column_name",
                    usage="모니터링", confidence=0.9,
                )
            return await super().structured(messages, model_cls, stage)

    builder = TableAssetContextRunner(deps=StubbornDeps())
    r = builder.build(tabular_data=DF, asset_name="process_log", source_description=SOURCE_DESC)
    assert TOP_KEYS.issubset(r.keys())
    assert r["issues"], "실패가 issues에 기록되지 않음"
    assert r["asset_context"] is not None, "부분 실패에도 산출물은 나와야 한다"
