"""에이전트 통합 테스트. pytest로 실행하거나 직접 실행한다."""

from __future__ import annotations

import asyncio


from agent.graph import build_agent
from agent.state import new_state
from fixtures import DF, SKILLS, SOURCE_DESC, MockDeps, make_state, print_trace


def run(deps, **kw):
    agent = build_agent(SKILLS, deps, **kw)
    return asyncio.get_event_loop().run_until_complete(
        agent.ainvoke(make_state(), {"recursion_limit": 200})
    )


# ---------------------------------------------------------------------------


def test_reaches_goal():
    """목표 슬롯까지 도달하고 정상 종료한다."""
    r = run(MockDeps())
    assert r["stop_reason"] == "goal_reached"
    assert r["board"]["values"].get("asset_context") is not None


def test_all_columns_interpreted():
    """모든 컬럼이 해석된다."""
    r = run(MockDeps())
    ctx = r["board"]["values"]["asset_context"]
    assert {c["name"] for c in ctx["columns"]} == set(DF.columns)


def _column_waves(result) -> int:
    """컬럼 해석이 몇 번의 파도로 끝났는지 센다."""
    iters = {
        h["iteration"] for h in result["history"]
        if h.get("ok") and "column_semantics" in (h.get("slots") or [])
    }
    return len(iters)


def test_batches_column_work():
    """컬럼 5개가 한 파도로 처리된다. 순차였다면 5파도가 필요하다."""
    r = run(MockDeps())
    plans = [h for h in r["history"] if h.get("phase") == "plan" and h.get("chosen")]
    assert any(len(p["chosen"]) > 1 for p in plans), "배치가 전혀 형성되지 않음"
    assert _column_waves(r) == 1, f"컬럼 해석이 {_column_waves(r)}파도로 나뉨"


def test_batching_scales_with_column_count():
    """
    컬럼이 늘어도 파도 수는 batch_limit에 비례해 늘 뿐, 컬럼 수에 비례하지 않는다.
    이 성질이 깨지면 넓은 테이블에서 왕복 비용이 폭발한다.
    """
    import pandas as pd

    import fixtures

    wide = pd.DataFrame({f"code_{i}": ["A", "B", "A", "B"] for i in range(20)})
    original = fixtures.DF
    fixtures.DF = wide
    try:
        r = run(MockDeps(), batch_limit=8)
    finally:
        fixtures.DF = original

    waves = _column_waves(r)
    assert waves <= 4, f"20컬럼이 {waves}파도로 처리됨 (batch_limit=8 기준 3파도 예상)"


def test_probe_refutes_false_primary_key():
    """
    equipment_id를 primary로 주장하면 uniqueness probe가 반증한다.
    handler의 if문이 아니라 데이터 조회로 잡히는지 확인한다.
    """
    r = run(MockDeps())
    acts = [h for h in r["history"] if h.get("skill") == "identifier-link" and h.get("target") == "equipment_id"]
    assert acts and acts[0]["attempts"] >= 2, "잘못된 primary 주장이 1차에 통과함"
    trail = " ".join(acts[0].get("trail", []))
    assert "probe" in trail and "유일성" in trail


def test_probe_refutes_time_resolution():
    """날짜만 있는 컬럼에 Hour 해상도를 주장하면 반증된다."""
    r = run(MockDeps())
    acts = [h for h in r["history"] if h.get("skill") == "temporal-axis"]
    assert acts and acts[0]["attempts"] >= 2
    assert "Day" in " ".join(acts[0].get("trail", [])) or acts[0]["attempts"] >= 2


def test_probe_refutes_unseen_category():
    """표본에 없는 카테고리 값을 설명하면 반증된다."""
    r = run(MockDeps())
    acts = [h for h in r["history"] if h.get("skill") == "categorical-code"]
    assert acts and acts[0]["attempts"] >= 2
    assert "보류" in " ".join(acts[0].get("trail", []))


def test_verification_report_present():
    """검증 리포트가 생성되고 미검증 주장이 구분된다."""
    r = run(MockDeps())
    v = r["board"]["values"].get("verification")
    assert v is not None
    assert v["status"] == "clean"
    assert v["checked_count"] > 0
    assert len(v["unverified"]) > 0, "자연어 주장이 unverified로 분류되지 않음"


def test_budget_tracked():
    r = run(MockDeps())
    assert r["budget"]["llm_calls"] > 0
    assert r["budget"]["probe_runs"] > 0


# ---------------------------------------------------------------------------


class StubbornDeps(MockDeps):
    """numeric-measure가 재시도해도 계속 근거 없는 단위를 고집한다."""

    async def structured(self, messages, model_cls, stage):
        if model_cls.__name__ == "MeasureOut":
            self.calls += 1
            return model_cls(
                meaning="공정 중 측정된 출력값이다.", unit="kPa", unit_evidence="column_name",
                usage="모니터링", confidence=0.9,
            )
        return await super().structured(messages, model_cls, stage)


def test_failure_isolated_and_partial_output():
    """한 컬럼이 계속 실패해도 격리되고 나머지로 산출물이 나온다."""
    r = run(StubbornDeps())
    assert "numeric-measure::power_value" in r["blocked"]
    ctx = r["board"]["values"].get("asset_context")
    assert ctx is not None, "일부 실패에도 최종 산출물은 나와야 한다"
    assert "power_value" not in {c["name"] for c in ctx["columns"]}
    assert ctx["coverage"].startswith("4/5")


class BudgetDeps(MockDeps):
    pass


def test_llm_budget_stops_run():
    agent = build_agent(SKILLS, BudgetDeps())
    state = new_state(
        asset_id="t", asset_name="process_log", data_ref="runtime://table-001",
        source_description=SOURCE_DESC, max_llm_calls=3,
    )
    r = asyncio.get_event_loop().run_until_complete(agent.ainvoke(state, {"recursion_limit": 200}))
    assert r["stop_reason"] == "llm_budget_exhausted"


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    deps = MockDeps()
    r = run(deps)
    print("=== 실행 궤적 ===")
    print_trace(r)
    print(f"\n종료: {r['stop_reason']} | iterations={r['iteration']} | "
          f"LLM={r['budget']['llm_calls']} probe={r['budget']['probe_runs']}")
    v = r["board"]["values"]["verification"]
    print(f"\n=== 검증 리포트 ===")
    print(f"통과 {len(v['verified'])} / 반증 {len(v['refuted'])} / 검증불가 {len(v['unverified'])} "
          f"| probe 검증률 {v['coverage']}")
    for e in v["verified"][:4]:
        print(f"  [OK ] {e['statement']}")
    for e in v["unverified"][:3]:
        print(f"  [ ? ] {e['statement'][:60]} ({e['detail'][:30]})")
