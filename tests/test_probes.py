"""probe는 반증 도구다. 통과가 참을 증명하지 않고, 실패가 거짓을 증명한다.
그리고 **실행 실패는 반증이 아니다** - 평가할 수 없으면 check를 건드리지 않는다."""

from __future__ import annotations

import pandas as pd
import pytest

from column_semantics.core.probes import (
    ProbeExpressionError,
    apply_probes,
    eval_probe_expression,
    run_probe,
    with_measurements,
)


def test_expression_evaluator_rejects_arbitrary_code():
    with pytest.raises(ProbeExpressionError):
        eval_probe_expression("__import__('os').system('ls')", {})
    with pytest.raises(ProbeExpressionError):
        eval_probe_expression("unknown_col + 1", {})


def test_probe_measures_comparison_ratio(equipment_df):
    observed = run_probe(
        equipment_df,
        {"expression": "v <= lim", "columns": {"v": "power_value", "lim": "power_limit"}},
    )
    assert observed["n"] == 12
    assert 0 < observed["true_ratio"] < 1  # 일부러 초과값을 넣은 표본


def test_probe_returns_none_when_column_missing(equipment_df):
    assert run_probe(equipment_df, {"expression": "a + b", "columns": {"a": "nope", "b": "power_value"}}) is None


def test_probe_returns_none_on_bad_expression(equipment_df):
    assert run_probe(equipment_df, {"expression": "v <=", "columns": {"v": "power_value"}}) is None


def test_probe_refutes_llm_claim(equipment_df):
    """LLM이 pass라고 써도 실측이 어긋나면 fail로 내려가야 한다."""
    validation = {
        "overall_status": "pass",
        "checks": [
            {
                "hypothesis": "power_value는 항상 power_limit 이하다",
                "status": "pass",
                "probe": {
                    "expression": "v <= lim",
                    "columns": {"v": "power_value", "lim": "power_limit"},
                },
            }
        ],
    }
    probe_log = []
    out = apply_probes(equipment_df, validation, probe_log)
    check = out["checks"][0]
    assert check["status"] == "fail"
    assert check["probe_verified"] is True
    # 실측값은 check가 아니라 probe_log에 남는다(rulebase 문서로 간다).
    assert "observed" not in check
    assert check["probe_id"] == probe_log[0]["probe_id"]
    assert probe_log[0]["observed"]["true_ratio"] < 0.95
    assert out["overall_status"] == "needs_revision"


def test_failed_check_gets_its_measurement_back_for_the_retry(equipment_df):
    """저장은 갈라놓지만, LLM에 되돌려주는 피드백에는 실측값이 붙어야 한다."""
    validation = {
        "checks": [
            {
                "hypothesis": "power_value는 항상 power_limit 이하다",
                "status": "pass",
                "observed": "LLM이 쓴 서술",
                "probe": {
                    "expression": "v <= lim",
                    "columns": {"v": "power_value", "lim": "power_limit"},
                },
            }
        ]
    }
    probe_log = []
    out = apply_probes(equipment_df, validation, probe_log)
    hinted = with_measurements(out["checks"], probe_log)[0]
    assert hinted["measured"]["true_ratio"] < 0.95
    # skill이 쓴 서술형 observed를 실측값이 덮지 않는다.
    assert hinted["observed"] == "LLM이 쓴 서술"


def test_probe_failure_to_run_leaves_claim_untouched(equipment_df):
    validation = {
        "overall_status": "pass",
        "checks": [
            {
                "hypothesis": "검사할 수 없는 주장",
                "status": "pass",
                "probe": {"expression": "v <= lim", "columns": {"v": "power_value", "lim": "없는컬럼"}},
            }
        ],
    }
    probe_log = []
    out = apply_probes(equipment_df, validation, probe_log)
    check = out["checks"][0]
    assert check["status"] == "pass"
    assert "probe_verified" not in check
    assert probe_log == []
    assert out["overall_status"] == "pass"


def test_tolerance_probe_reports_target_and_ratio():
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [2.0, 4.0, 6.0]})
    observed = run_probe(
        df, {"expression": "b / a", "columns": {"a": "a", "b": "b"}, "target": 2.0, "tolerance": 0.01}
    )
    assert observed["within_tolerance_ratio"] == 1.0
    assert observed["target"] == 2.0
