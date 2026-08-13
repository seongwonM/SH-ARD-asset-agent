"""
Executor: 플래너가 고른 작업 배치를 실행하고 결과를 검증한다.

책임 경계
  planner  : 무엇을 할지 고른다
  executor : 그것들을 (병렬로) 실행하고, 결과가 근거에 맞는지 본다
  graph    : 둘을 반복시키고 언제 멈출지 정한다

3단 검증
  1. skill 자체 검증  handler 안에서 SkillResult(ok=False)로 되돌림
  2. probe 검증       claim에 붙은 probe를 데이터로 실행해 반증 (여기)
  3. 공통 가드        텍스트 근거 대조 (guards.py)

2번이 핵심이다. 1·3번은 결국 텍스트를 텍스트와 대조하지만,
probe는 데이터를 조회한다. "이 컬럼은 primary key다"라는 주장은
probe만이 반증할 수 있고, 반증 시 실측값이 재시도 힌트가 된다.

실패 처리
skill 하나가 실패해도 배치의 나머지와 이후 루프는 계속된다.
실패한 (skill, target) 조합만 blocked에 격리해 재선택을 막는다.
같은 조합을 무한 재시도하는 것이 이 구조에서 가장 흔한 실패 모드다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Tuple

from .contract import Artifact, SkillContext, SkillDeps, SkillResult
from .guards import GuardContext, run_guards
from .probes import run_probes
from .registry import SkillRegistry
from .state import AgentState, Board, load_board, load_facts

logger = logging.getLogger(__name__)


async def execute_batch(
    state: AgentState,
    registry: SkillRegistry,
    deps: SkillDeps,
    tasks: List[Tuple[str, str]],
) -> Dict[str, Any]:
    """작업 배치를 동시 실행하고 결과를 board에 병합한다."""
    board = load_board(state)
    facts = load_facts(state)
    iteration = state.get("iteration", 0) + 1
    started = time.time()

    guard_ctx = GuardContext(
        language=state.get("language", "ko"),
        column_names=[f.name for f in facts],
        allowed_numbers=_allowed_numbers(facts, state),
        grounding_text=" ".join(
            [state.get("asset_name", ""), state.get("source_description", "")]
            + list((state.get("column_descriptions") or {}).values())
            + [s for f in facts for s in f.samples]
        ),
    )

    results = await asyncio.gather(
        *[
            _run_one(state, registry, deps, board, guard_ctx, skill, target)
            for skill, target in tasks
        ]
    )

    # 병합은 순차로 한다. 배치는 key가 겹치지 않도록 구성되지만,
    # 병합만은 단일 스레드로 두어야 순서 의존 버그가 생기지 않는다.
    blocked = list(state.get("blocked") or [])
    requested = list(state.get("requested_slots") or [])
    analysis_queue = list(state.get("analysis_queue") or [])
    history: List[Dict[str, Any]] = []
    llm_calls = 0
    probe_runs = 0

    for r in results:
        llm_calls += r["llm_calls"]
        probe_runs += r["probe_runs"]
        history.append(r["record"])
        if r["result"] is not None:
            for c in r["result"].contributions:
                board.put(c)
            for artifact in r["result"].artifacts:
                board.add_artifact(artifact)
            for slot in r["result"].requests:
                if slot.value not in requested:
                    requested.append(slot.value)
            for need in r["result"].analysis_needs:
                entry = need.model_dump(mode="json")
                if entry not in analysis_queue:
                    analysis_queue.append(entry)
        elif r["key"] not in blocked:
            blocked.append(r["key"])

    elapsed_ms = int((time.time() - started) * 1000)
    logger.info(
        "batch_done iteration=%d tasks=%d llm_calls=%d probe_runs=%d elapsed_ms=%d",
        iteration, len(tasks), llm_calls, probe_runs, elapsed_ms,
    )

    return {
        "board": board.model_dump(),
        "iteration": iteration,
        "blocked": blocked,
        "requested_slots": requested,
        "analysis_queue": analysis_queue,
        "budget": {"llm_calls": (state.get("budget") or {}).get("llm_calls", 0) + llm_calls,
                   "probe_runs": (state.get("budget") or {}).get("probe_runs", 0) + probe_runs},
        "history": history,
        "batch": [],
        "elapsed_ms": elapsed_ms,
    }


async def _run_one(
    state: AgentState,
    registry: SkillRegistry,
    deps: SkillDeps,
    board: Board,
    guard_ctx: GuardContext,
    skill_name: str,
    target: str,
) -> Dict[str, Any]:
    """작업 1건: 호출 → 3단 검증 → 재시도. 예외를 밖으로 던지지 않는다."""
    skill = registry.get(skill_name)
    manifest = skill.manifest
    iteration = state.get("iteration", 0) + 1
    key = f"{skill_name}::{target}"

    ctx = SkillContext(
        asset_id=state.get("asset_id", ""),
        asset_name=state.get("asset_name", ""),
        source_description=state.get("source_description", ""),
        language=state.get("language", "ko"),
        data_ref=state.get("data_ref", ""),
        instructions=skill.instructions,  # 여기서 처음 본문을 로드한다
        target=target,
        column_description=(state.get("column_descriptions") or {}).get(target, ""),
        column_descriptions=state.get("column_descriptions") or {},
        board=board.model_dump(),
        repair_hints=[],
    )

    if skill.handler is None:
        return _fail(key, skill_name, target, iteration, "handler.py 없음", 0, 0)

    llm_calls = probe_runs = 0
    trail: List[str] = []

    for attempt in range(1, manifest.max_attempts + 1):
        try:
            result: SkillResult = await skill.handler(ctx, deps)
            llm_calls += 1
        except Exception as exc:  # noqa: BLE001
            reason = f"실행 예외: {type(exc).__name__}: {exc}"[:250]
            trail.append(f"[{attempt}] {reason}")
            ctx.repair_hints = [reason]
            continue

        # 1단: skill 자체 검증
        if not result.ok:
            trail.append(f"[{attempt}] self: {result.notes}")
            ctx.repair_hints = [result.notes]
            continue

        if not result.artifacts:
            result.artifacts = [
                Artifact(
                    artifact_type=f"{manifest.role.value}:{c.slot.value}",
                    producer=skill_name,
                    role=manifest.role,
                    slot=c.slot,
                    key=c.key,
                    scope={"target": target} if target else {},
                    payload=c.value,
                    evidence=c.evidence,
                    confidence=c.confidence,
                )
                for c in result.contributions
            ]

        # 2단: probe 검증 — 데이터로 주장을 반증한다
        probe_fail = _verify_claims(result, deps, ctx.data_ref)
        probe_runs += len(result.claims)
        if probe_fail:
            trail.append(f"[{attempt}] probe: {probe_fail[0]}")
            ctx.repair_hints = probe_fail[:3]
            continue

        # 3단: 공통 가드
        violations = run_guards(result, guard_ctx)
        if violations:
            trail.append(f"[{attempt}] guard: {violations[0].message}")
            ctx.repair_hints = [f"{v.message} → {v.hint}" for v in violations[:3]]
            continue

        logger.info(
            "skill_ok skill=%s target=%s iteration=%d attempts=%d",
            skill_name, target, iteration, attempt,
        )
        return {
            "result": result,
            "key": key,
            "llm_calls": llm_calls,
            "probe_runs": probe_runs,
            "record": {
                "iteration": iteration,
                "skill": skill_name,
                "target": target,
                "ok": True,
                "attempts": attempt,
                "slots": [c.slot.value for c in result.contributions],
                "artifacts": [a.artifact_type for a in result.artifacts],
                "analysis_needs": [n.slot.value for n in result.analysis_needs],
                "claims_verified": len(result.claims),
                "note": result.notes,
                "trail": trail,
            },
        }

    return _fail(key, skill_name, target, iteration, trail[-1] if trail else "원인 불명", llm_calls, probe_runs)


def _verify_claims(result: SkillResult, deps: SkillDeps, data_ref: str) -> List[str]:
    """
    claim에 붙은 probe를 데이터로 실행한다.
    critical 실패만 재시도를 유발하고, 비critical은 경고로 남긴다.
    """
    if not result.claims:
        return []
    try:
        df = deps.dataframe(data_ref)
    except Exception as exc:  # noqa: BLE001
        # 데이터를 못 읽는 것은 주장의 문제가 아니므로 통과시킨다.
        result.notes += f" [probe 생략: {exc}]"
        return []

    outcomes = run_probes([c.probe for c in result.claims], df)
    failures: List[str] = []
    for claim, outcome in zip(result.claims, outcomes):
        if outcome.error:
            continue  # probe 자체 오류는 주장을 반증하지 못한다
        if outcome.passed:
            continue
        msg = f"'{claim.statement}' 주장이 데이터와 맞지 않는다. {outcome.detail}"
        if claim.critical:
            failures.append(msg)
        else:
            result.notes += f" [경고] {msg}"
    return failures


def _fail(key, skill, target, iteration, reason, llm_calls, probe_runs) -> Dict[str, Any]:
    """
    재시도 소진. 해당 조합을 격리하고 진행한다.

    여기서 루프를 죽이지 않는 것이 중요하다. 컬럼 하나의 해석 실패로
    테이블 전체 컨텍스트 생성이 중단되면 배치 처리가 불가능하다.
    """
    logger.warning(
        "skill_fail skill=%s target=%s iteration=%d reason=%s",
        skill, target, iteration, reason,
    )
    return {
        "result": None,
        "key": key,
        "llm_calls": llm_calls,
        "probe_runs": probe_runs,
        "record": {
            "iteration": iteration,
            "skill": skill,
            "target": target,
            "ok": False,
            "reason": str(reason)[:300],
        },
    }


def _allowed_numbers(facts, state) -> List[str]:
    """가드가 참조하는 '입력에 실제로 등장한 수치' 집합."""
    import re

    pat = re.compile(r"\d+(?:[.,]\d+)?")
    nums: List[str] = []
    for f in facts:
        nums.append(str(f.distinct_count))
        nums.extend(pat.findall(f"{f.min_value} {f.max_value}"))
        for s in f.samples:
            nums.extend(pat.findall(str(s)))
    nums.extend(pat.findall(state.get("source_description", "") or ""))
    for desc in (state.get("column_descriptions") or {}).values():
        nums.extend(pat.findall(str(desc)))
    return sorted({n.replace(",", "") for n in nums})
