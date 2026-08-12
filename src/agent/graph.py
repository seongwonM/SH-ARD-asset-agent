"""
LangGraph 조립.

    START → plan ──(done)──→ END
              │
            (배치 선택됨)
              ↓
            act ─────────────→ plan   (루프)

노드가 2개뿐인 이유:
파이프라인 단계를 노드로 만들면 skill을 추가할 때마다 그래프를 고쳐야 한다.
여기서는 '무엇을 할지 정한다'와 '한다' 둘만 노드이고,
실제 작업 종류는 skills/ 폴더가 결정한다. 그래프는 skill 개수와 무관하게 고정이다.

act는 배치를 받아 동시 실행한다. 컬럼 200개도 iteration 200번이 아니라
파도(wave) 몇 번으로 끝난다.

종료 조건 (planner.decide)
  goal_reached                 목표 슬롯 충족
  iteration_budget_exhausted   max_iterations 도달
  llm_budget_exhausted         LLM 호출 예산 소진
  no_applicable_skill          남은 gap에 적용 가능한 skill 없음
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from .contract import SkillDeps
from .executor import execute_batch
from .planner import MAX_BATCH, decide
from .registry import SkillRegistry
from .state import AgentState


def build_agent(
    skills_dir: Path,
    deps: SkillDeps,
    checkpointer: Any = None,
    batch_limit: int = MAX_BATCH,
):
    registry = SkillRegistry(skills_dir)
    if registry.errors:
        raise ValueError("SKILL.md 로딩 실패:\n" + "\n".join(registry.errors))

    def plan_node(state: AgentState) -> Dict[str, Any]:
        d = decide(state, registry, batch_limit)
        return {
            "batch": [{"skill": c.skill, "target": c.target} for c in d.batch],
            "plan_note": d.note,
            "done": d.done,
            "stop_reason": d.stop_reason,
            "history": [
                {
                    "iteration": state.get("iteration", 0),
                    "phase": "plan",
                    "note": d.note,
                    "chosen": [f"{c.skill}::{c.target}" for c in d.batch],
                    "considered": [c.model_dump(mode="json") for c in d.considered],
                    "needs_tiebreak": d.needs_tiebreak,
                }
            ],
        }

    async def act_node(state: AgentState) -> Dict[str, Any]:
        tasks = [(b["skill"], b.get("target", "")) for b in state.get("batch", [])]
        return await execute_batch(state, registry, deps, tasks)

    g = StateGraph(AgentState)
    g.add_node("plan", plan_node)
    g.add_node("act", act_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges(
        "plan",
        lambda s: END if s.get("done") or not s.get("batch") else "act",
        {END: END, "act": "act"},
    )
    g.add_edge("act", "plan")

    return g.compile(checkpointer=checkpointer)
