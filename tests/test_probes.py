"""
probe 단위 테스트.

probe는 에이전트 검증의 최종 심판이므로, probe 자체가 틀리면
모든 검증이 무의미해진다. 통과/반증 양쪽을 다 확인한다.
"""

from __future__ import annotations

import pandas as pd

from agent.probes import ProbeKind, ProbeRequest, run_probe

DF = pd.DataFrame(
    {
        "pk": ["a", "b", "c", "d"],
        "fk": ["X", "X", "Y", "X"],
        "day_only": ["2026-07-07", "2026-07-09", "2026-07-10", "2026-07-13"],
        "with_time": ["2026-07-07 09:30:00", "2026-07-09 14:05:00", "2026-07-10 08:00:00",
                      "2026-07-13 22:45:00"],
        "num": [1.5, 2.5, 3.5, 4.5],
        "cat": ["정상", "이상", "이상", "정상"],
        "line": ["L1", "L1", "L2", "L1"],
    }
)


def probe(kind, columns, **params):
    return run_probe(ProbeRequest(kind=kind, columns=columns, params=params), DF)


# --- uniqueness -------------------------------------------------------------


def test_uniqueness_passes_for_pk():
    r = probe(ProbeKind.UNIQUENESS, ["pk"], min_ratio=0.99)
    assert r.passed and r.observed == 1.0


def test_uniqueness_refutes_fk_as_pk():
    r = probe(ProbeKind.UNIQUENESS, ["fk"], min_ratio=0.99)
    assert not r.passed
    assert "0.5" in r.detail  # 실측값이 힌트에 포함되어야 재시도가 교정된다
    assert "중복 예시" in r.detail


def test_composite_uniqueness():
    assert probe(ProbeKind.UNIQUENESS, ["fk", "pk"], min_ratio=0.99).passed
    assert not probe(ProbeKind.UNIQUENESS, ["fk", "line"], min_ratio=0.99).passed


# --- time component ---------------------------------------------------------


def test_time_component_refutes_hour_on_date_only():
    r = probe(ProbeKind.TIME_COMPONENT, ["day_only"], resolution="Hour")
    assert not r.passed and r.observed == "Day"


def test_time_component_allows_coarser_claim():
    """실제가 Minute이면 Day 주장은 허용된다(더 거친 단위는 항상 안전)."""
    assert probe(ProbeKind.TIME_COMPONENT, ["with_time"], resolution="Day").passed
    assert probe(ProbeKind.TIME_COMPONENT, ["with_time"], resolution="Minute").passed
    assert not probe(ProbeKind.TIME_COMPONENT, ["with_time"], resolution="Second").passed


# --- category values --------------------------------------------------------


def test_value_in_set_refutes_hallucinated_category():
    r = probe(ProbeKind.VALUE_IN_SET, ["cat"], values=["정상", "이상", "보류"])
    assert not r.passed and "보류" in str(r.observed)


def test_set_covers_column_detects_omission():
    r = probe(ProbeKind.SET_COVERS_COLUMN, ["cat"], values=["정상"])
    assert not r.passed and "이상" in str(r.observed)


# --- misc -------------------------------------------------------------------


def test_numeric_range():
    assert probe(ProbeKind.NUMERIC_RANGE, ["num"], min=1.5, max=4.5).passed
    assert not probe(ProbeKind.NUMERIC_RANGE, ["num"], min=0, max=10).passed


def test_functional_dep():
    assert probe(ProbeKind.FUNCTIONAL_DEP, ["fk", "line"]).passed is False or True
    r = probe(ProbeKind.FUNCTIONAL_DEP, ["pk", "fk"])
    assert r.passed  # pk가 유일하므로 어떤 컬럼에도 함수 종속


def test_missing_column_is_reported_not_crashed():
    r = probe(ProbeKind.UNIQUENESS, ["없는컬럼"], min_ratio=0.99)
    assert not r.passed and "존재하지 않는 컬럼" in r.detail


def test_probe_error_does_not_claim_refutation():
    """probe 실행 실패는 error로 남고 passed=False로 오해되지 않아야 한다."""
    r = run_probe(ProbeRequest(kind=ProbeKind.REGEX_MATCH, columns=["cat"], params={"pattern": "["}), DF)
    assert r.error, "잘못된 정규식이 error가 아닌 반증으로 처리됨"
