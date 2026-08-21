"""결과 문서 6벌 -> Markdown 렌더러.

여기서 확인하는 것은 서식이 아니라 **조인**이다. 저장할 때 id로만 이어둔 것
(check -> probe 실측, 컬럼 -> check)이 보고서에서 실제로 이어지는지, 그리고
문서가 일부만 있는 실행(중간에 죽은 배치)에서도 렌더가 살아남는지.
"""

from __future__ import annotations

import json
from pathlib import Path

import report_md

META = {
    "status": "done",
    "validation_status": "pass",
    "llm_model": "test-model",
    "source_csv": "/data/equipment.csv",
    "row_count": 10,
    "column_count": 2,
    "elapsed_seconds": 1.5,
    "max_rounds": 2,
    "max_gap_rounds": 2,
    "max_actions_per_column": 2,
    "max_group_columns": 4,
}


def _documents() -> dict:
    return {
        "columns": {
            "meta": {**META, "part": "columns"},
            "columns": {
                "qty": {
                    "final": {
                        "semantic_type": {"type": "count", "confidence": 0.8},
                        "interpretation": {
                            "selected_meaning": {"meaning": "생산 수량", "unit": None},
                            "semantic_type": {"type": "count", "confidence": 0.8},
                            "meaning_candidates": [
                                {"meaning": "생산 수량", "evidence": ["이름이 qty"], "counter_evidence": []}
                            ],
                            "domain_gap": None,
                            "status": "resolved",
                        },
                        "validated": None,
                    },
                    "stages": [
                        {
                            "stage": "gap",
                            "before": {"selected_meaning": {"meaning": "수량"}},
                            "after": {"selected_meaning": {"meaning": "생산 수량"}},
                            "changed": ["selected_meaning"],
                            "skill": "reconsider_ambiguous",
                            "reason": "단위가 붙지 않았다",
                            "phase": "exec",
                            "round": 1,
                        },
                    ],
                },
                "ratio": {
                    "final": {
                        "semantic_type": {"type": "measurement"},
                        "interpretation": {
                            "selected_meaning": {"meaning": "비율", "unit": "fraction_0_1"},
                            "semantic_type": {"type": "measurement"},
                            "domain_gap": {
                                "missing": "어느 공정의 비율인지",
                                "why": "공정 코드가 테이블에 없다",
                                "would_resolve": ["공정 마스터 표"],
                            },
                            "status": "resolved",
                        },
                        "validated": None,
                    },
                    "stages": [],
                },
            },
        },
        "rulebase": {
            "meta": {**META, "part": "rulebase"},
            "table": {"row_count": 10, "column_count": 2, "columns": ["qty", "ratio"]},
            "column_profiles": {
                "qty": {
                    "physical_type": "integer",
                    "null_ratio": 0.0,
                    "nunique": 10,
                    "sample_values": [1, 2, 3],
                }
            },
            "relation_evidence": {"pairwise": []},
            "grain_candidates": [],
            "probes": [
                {
                    "probe_id": "probe-1",
                    "check_id": "r1-c1",
                    "requested": {"expression": "a >= 0", "columns": {"a": "qty"}},
                    "observed": {"expression": "a >= 0", "columns": {"a": "qty"}, "n": 10, "true_ratio": 1.0},
                    "not_evaluable": None,
                },
                {
                    "probe_id": "probe-2",
                    "check_id": "r1-c2",
                    "requested": {"expression": "a <= 1", "columns": {"a": "ratio"}},
                    "observed": None,
                    "not_evaluable": "숫자로 읽히는 행이 2개뿐(3개 미만)",
                },
            ],
        },
        "plan": {
            "meta": {**META, "part": "plan"},
            "first_pass": {
                "stages": ["column_interpretation"],
                "relation_analysis_included": False,
                "reason": "pairwise 증거가 없어 relation_analysis를 생략했다",
            },
            "gap_rounds": [
                {
                    "round": 1,
                    "gate": "domain_gap",
                    "considered": ["qty", "ratio"],
                    "flagged": ["qty"],
                    "planner": {"raw": "...", "skipped": None},
                    "actions": [
                        {"action": "reconsider_ambiguous", "columns": ["qty"], "reason": "단위가 붙지 않았다"}
                    ],
                    "dropped": [
                        {"action": "joint_interpretation", "columns": ["qty"], "why": "여러 컬럼을 보는 행동인데 컬럼이 하나"}
                    ],
                    "changed": ["qty"],
                }
            ],
            "replans": [],
            "execution": [
                {"event": "stage", "stage": "column_interpretation", "phase": "exec", "elapsed_seconds": 0.4}
            ],
        },
        "table": {
            "meta": {**META, "part": "table"},
            "table_context": {
                "asset_context": "설비별 생산 실적 테이블",
                "row_grain": {"description": "설비-일자", "columns": ["qty"], "confidence": 0.7},
                "entities": [{"name": "설비", "columns": ["qty"], "role": "주체", "confidence": 0.6}],
                "measures": [],
                "uncertainties": ["단위 표기가 없다"],
            },
            "relation_analysis": None,
            "joint_findings": [],
            "validation": {
                "final_status": "pass",
                "rounds": [
                    {
                        "round": 1,
                        "phase": "exec",
                        "overall_status": "pass",
                        "checks": [
                            {
                                "check_id": "r1-c1",
                                "hypothesis": "수량은 음수가 아니다",
                                "columns": ["qty"],
                                "status": "pass",
                                "severity": "medium",
                                "probe_id": "probe-1",
                            },
                            {
                                "check_id": "r1-c2",
                                "hypothesis": "비율은 1 이하다",
                                "columns": ["ratio"],
                                "status": "pass",
                                "severity": "low",
                                "probe_id": "probe-2",
                            },
                        ],
                        "revision_requests": [],
                    }
                ],
            },
        },
        "lean": {
            "meta": {**META, "part": "lean"},
            "enabled": True,
            "stages": {
                "column_interpretation": {
                    "qty": {"meaning": "생산된 수량", "unit": None, "unknown": None},
                    "ratio": {
                        "meaning": "비율값",
                        "unit": "fraction_0_1",
                        "unknown": "어느 공정의 비율인지",
                    },
                },
                "table_context": {
                    "table": {"asset_context": "생산 실적", "row_grain": "설비-일자 1건"}
                },
                "semantic_validation": {
                    "group1": {"wrong_meanings": [{"columns": ["ratio"], "why": "최댓값이 87이다"}]}
                },
            },
            "entries": [
                {
                    "stage": "table_context",
                    "target": "table",
                    "output": None,
                    "error": "RuntimeError: 최소 출력 실패",
                }
            ],
        },
        "llm_calls": {
            "meta": {**META, "part": "llm_calls"},
            "prompts": {"column_interpretation": "system prompt text"},
            "calls": [
                {
                    "seq": 1,
                    "prompt_ref": "column_interpretation",
                    "label": "column_interpretation:qty",
                    "status": "ok",
                    "input_chars": 100,
                    "output_chars": 50,
                    "tokens": 30,
                    "elapsed_seconds": 0.4,
                    "input": {"target_column": "qty"},
                    "output": {"status": "resolved"},
                },
                {
                    "seq": 2,
                    "prompt_ref": "column_review",
                    "label": "column_review:ratio",
                    "status": "error",
                    "attempt": 2,
                    "error": "TimeoutError: read timeout",
                    "elapsed_seconds": 30.0,
                    "input": {},
                    "output": None,
                },
            ],
        },
    }


def _write(tmp_path: Path, docs: dict, base_name: str = "result.semantic") -> Path:
    for part, body in docs.items():
        (tmp_path / f"{base_name}.{part}.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )
    return tmp_path / base_name


def test_renders_full_run(tmp_path):
    base = _write(tmp_path, _documents())
    md = report_md.render(report_md.load_documents(base), "제목", include_calls=False, detail=True)

    assert "# 제목" in md
    assert "생산 수량" in md
    # check -> probe: 실측값이 가설 옆에 와야 한다(문서에서는 probe_id로만 이어져 있다)
    assert "참 100.0%" in md
    # 재보지 못한 probe는 통과로 보이면 안 된다
    assert "재보지 못함" in md
    assert "숫자로 읽히는 행이 2개뿐(3개 미만)" in md
    # 버린 행동은 이유와 함께 남는다
    assert "여러 컬럼을 보는 행동인데 컬럼이 하나" in md
    # domain_gap은 접히지 않고 그대로 보인다
    assert "어느 공정의 비율인지" in md
    assert "공정 마스터 표" in md
    # 단계 이력
    assert "domain_gap" in md
    assert "reconsider_ambiguous" in md
    # 실패한 호출
    assert "TimeoutError: read timeout" in md


def test_column_to_check_join(tmp_path):
    base = _write(tmp_path, _documents())
    md = report_md.render(report_md.load_documents(base), "t", include_calls=False, detail=True)

    qty_section = md.split("#### `qty`")[1].split("#### ")[0]
    assert "r1-c1" in qty_section
    assert "참 100.0%" in qty_section
    # 다른 컬럼의 check가 섞여 들어오면 안 된다
    assert "r1-c2" not in qty_section


def test_partial_run_still_renders(tmp_path):
    docs = _documents()
    base = _write(tmp_path, {"columns": docs["columns"]})
    md = report_md.render(report_md.load_documents(base), "t", include_calls=False, detail=True)

    assert "없는 문서" in md
    assert "생산 수량" in md


def test_unfinished_run_is_called_out(tmp_path):
    docs = _documents()
    docs["columns"]["meta"] = {**META, "part": "columns", "status": "failed", "error": "RuntimeError: boom"}
    base = _write(tmp_path, {"columns": docs["columns"]})
    md = report_md.render(report_md.load_documents(base), "t", include_calls=False, detail=False)

    assert "완주하지 않은 실행" in md
    assert "RuntimeError: boom" in md


def test_lean_output_sits_next_to_the_full_one(tmp_path):
    """이 절의 존재 이유가 나란함이다 - 같은 컬럼의 두 답이 한 줄에 있어야
    '분석용 필드를 빼도 의미가 그대로인가'를 눈으로 판단할 수 있다."""
    base = _write(tmp_path, _documents())
    md = report_md.render(report_md.load_documents(base), "t", include_calls=False, detail=False)

    section = md.split("## 최소 출력 비교")[1]
    qty_row = next(line for line in section.splitlines() if line.startswith("| qty "))
    assert "생산 수량" in qty_row  # 전체 출력
    assert "생산된 수량" in qty_row  # 최소 출력
    # 최소 출력이 스스로 남긴 '모르는 것'도 같은 줄에 온다.
    ratio_row = next(line for line in section.splitlines() if line.startswith("| ratio "))
    assert "어느 공정의 비율인지" in ratio_row

    assert "생산 실적" in section  # 테이블 의미
    assert "최댓값이 87이다" in section  # 최소 출력이 지적한 어긋남
    assert "최소 출력 호출 1건이 실패했다" in section


def test_lean_section_says_so_when_it_was_not_run(tmp_path):
    docs = _documents()
    docs["lean"] = {"meta": {**META, "part": "lean"}, "enabled": False, "stages": {}, "entries": []}
    base = _write(tmp_path, docs)
    md = report_md.render(report_md.load_documents(base), "t", include_calls=False, detail=False)

    assert "최소 출력을 받지 않았다" in md


def test_gap_rounds_from_older_runs_still_render(tmp_path):
    """게이트가 컬럼별 검토 호출이던 시절의 결과가 PVC에 남아 있다."""
    docs = _documents()
    docs["plan"]["gap_rounds"] = [
        {
            "round": 1,
            "reviewed": ["qty", "ratio"],
            "flagged": ["qty"],
            "malformed_reviews": ["ratio"],
            "planner": None,
            "actions": [],
            "dropped": [],
            "changed": [],
        }
    ]
    base = _write(tmp_path, docs)
    md = report_md.render(report_md.load_documents(base), "t", include_calls=False, detail=False)

    assert "게이트(column_review) 2개 판정" in md
    assert "형식을 못 맞춘 검토 응답 1건" in md


def test_resolve_base_from_directory(tmp_path):
    base = _write(tmp_path, _documents())
    assert report_md.resolve_base(tmp_path) == base


def test_include_calls_embeds_raw(tmp_path):
    base = _write(tmp_path, _documents())
    md = report_md.render(report_md.load_documents(base), "t", include_calls=True, detail=False)

    assert "호출 원문" in md
    assert "column_interpretation:qty" in md
