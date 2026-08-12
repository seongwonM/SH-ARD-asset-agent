"""
Planner: 현재 상태 → 남은 gap → 다음에 실행할 작업 배치.

단건이 아니라 배치를 반환한다.
컬럼 200개짜리 테이블에서 한 번에 하나씩 고르면 200번의 plan-act 왕복이 생기고,
그 중 199번은 "또 다음 컬럼"이라는 자명한 판단이다. 서로 독립인 작업은
한 파도(wave)로 묶어 동시에 실행한다.

배치 안전성 규칙
  - keyed 슬롯(column_semantics 등)에 서로 다른 key로 쓰는 작업만 묶는다.
    key가 다르면 쓰기 충돌이 없다.
  - 단일 값 슬롯에 쓰는 작업(grain, summary 등)은 항상 단독 실행한다.
    같은 슬롯을 두 skill이 동시에 쓰면 마지막 승자가 비결정적이다.
  - 한 컬럼에 두 skill이 동시에 붙지 않게 target 중복을 제거한다.

2단 구조
  1단(필수) 결정론적 필터: requires 충족 + applies_when 매칭 + gap 기여
  2단(선택) LLM tie-break: 1단 통과 후보가 여럿이고 점수가 비슷할 때만

LLM을 주 선택자로 쓰지 않는 이유:
"왜 이 컬럼에 이 skill이 갔나"에 답할 수 없으면 임계값 튜닝이 불가능하다.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from .contract import COST_WEIGHT, AppliesWhen, Cost, SkillRole, Slot
from .registry import SkillRegistry
from .state import AgentState, Board, ColumnFact, Gap

MAX_BATCH = 8  # 동시 실행 상한. deps의 LLM 동시성 제한과 맞춘다.

# 대상별로 채워지는 슬롯. 이 슬롯만 쓰는 작업은 병렬 안전하다.
KEYED_SLOTS = {
    Slot.COLUMN_SEMANTICS,
    Slot.DISTRIBUTION_PROFILE,
    Slot.VALUE_PATTERNS,
    Slot.JOIN_CANDIDATES,
    Slot.LINKAGE,
}

EVIDENCE_SLOTS = {
    Slot.TABLE_PROFILE,
    Slot.DISTRIBUTION_PROFILE,
    Slot.VALUE_PATTERNS,
    Slot.JOIN_CANDIDATES,
    Slot.CONSTRAINTS,
    Slot.QUALITY_RISKS,
    Slot.COMPLIANCE,
    Slot.GLOSSARY,
}


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    target: str = ""
    score: float = 0.0
    cost: Cost = Cost.MEDIUM
    provides: List[Slot] = Field(default_factory=list)
    keyed_only: bool = False
    reasons: List[str] = Field(default_factory=list)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch: List[Candidate] = Field(default_factory=list)
    note: str = ""
    done: bool = False
    stop_reason: str = ""
    considered: List[Candidate] = Field(default_factory=list)
    needs_tiebreak: bool = False


# ---------------------------------------------------------------------------
# 1. Gap 계산
# ---------------------------------------------------------------------------


def compute_gaps(
    board: Board, facts: List[ColumnFact], goal_slots: List[str], requested: List[str] | None = None
) -> List[Gap]:
    """
    목표 슬롯에서 출발해 필요한 선행 슬롯까지 역으로 펼친다.
    per_column 슬롯은 미처리 컬럼 목록을 targets에 담는다.
    requested는 skill이 실행 중 스스로 요청한 슬롯이다(SkillResult.requests).
    """
    gaps: List[Gap] = []

    if not board.has(Slot.TABLE_PROFILE):
        gaps.append(Gap(slot=Slot.TABLE_PROFILE))

    analysis_ready = board.has(Slot.ANALYSIS_PLAN)

    if facts:
        done = set(board.keys_of(Slot.COLUMN_SEMANTICS))
        missing = [f.name for f in facts if f.name not in done]
        if missing:
            blocked = [] if analysis_ready else [Slot.ANALYSIS_PLAN]
            gaps.append(Gap(slot=Slot.COLUMN_SEMANTICS, targets=missing, blocked_by=blocked))

    # 먼저 결정론적 evidence를 충분히 채운다.
    if board.has(Slot.TABLE_PROFILE):
        for slot in (
            Slot.DISTRIBUTION_PROFILE,
            Slot.VALUE_PATTERNS,
            Slot.JOIN_CANDIDATES,
            Slot.CONSTRAINTS,
            Slot.QUALITY_RISKS,
            Slot.COMPLIANCE,
            Slot.GLOSSARY,
        ):
            if not board.has(slot):
                gaps.append(Gap(slot=slot))

    # evidence가 어느 정도 쌓인 뒤에만 "다음에 뭘 더 볼지" 계획을 세운다.
    evidence_ready = all(board.has(slot) for slot in EVIDENCE_SLOTS if slot != Slot.GLOSSARY)
    if evidence_ready and not board.has(Slot.ANALYSIS_PLAN):
        gaps.append(Gap(slot=Slot.ANALYSIS_PLAN))

    semantic_ready = analysis_ready and bool(facts)
    for slot in (Slot.GRAIN, Slot.LINKAGE, Slot.TOPIC, Slot.SUMMARY, Slot.SEARCH_TERMS, Slot.VERIFICATION):
        if board.has(slot):
            continue
        blocked = [] if semantic_ready else [Slot.ANALYSIS_PLAN]
        gaps.append(Gap(slot=slot, blocked_by=blocked))

    for name in list(goal_slots) + list(requested or []):
        slot = Slot(name)
        if board.has(slot) or any(g.slot == slot for g in gaps):
            continue
        blocked = [s for s in (Slot.SUMMARY, Slot.TOPIC) if not board.has(s)]
        gaps.append(Gap(slot=slot, blocked_by=blocked))

    return gaps


# ---------------------------------------------------------------------------
# 2. 적용 조건 평가
# ---------------------------------------------------------------------------


def applies(
    cond: AppliesWhen, fact: Optional[ColumnFact], facts: List[ColumnFact], rows: int
) -> Tuple[bool, str]:
    if cond.always:
        return True, "always"
    if cond.min_columns and len(facts) < cond.min_columns:
        return False, f"컬럼 수 {len(facts)} < {cond.min_columns}"
    if cond.min_rows and rows < cond.min_rows:
        return False, f"행 수 {rows} < {cond.min_rows}"
    if cond.column_kinds:
        if fact is None:
            if not any(f.kind in cond.column_kinds for f in facts):
                return False, f"해당 종류 컬럼 없음 {[k.value for k in cond.column_kinds]}"
        elif fact.kind not in cond.column_kinds:
            return False, f"kind={fact.kind.value} 불일치"
    if cond.column_name_patterns and fact is not None:
        if not any(re.search(p, fact.name, re.I) for p in cond.column_name_patterns):
            return False, "컬럼명 패턴 불일치"
    return True, "조건 충족"


# ---------------------------------------------------------------------------
# 3. 후보 생성 및 점수화
# ---------------------------------------------------------------------------


def score_candidates(
    registry: SkillRegistry,
    board: Board,
    facts: List[ColumnFact],
    gaps: List[Gap],
    blocked: List[str],
    rows: int = 0,
    strict: bool = True,
    unreachable: set | None = None,
) -> List[Candidate]:
    open_gaps = [g for g in gaps if g.is_open]
    gap_slots = {g.slot for g in open_gaps}
    gap_targets: Dict[Slot, List[str]] = {g.slot: g.targets for g in open_gaps}
    incomplete = {g.slot for g in gaps}
    blocked_set = set(blocked)
    out: List[Candidate] = []

    for skill in registry.all():
        m = skill.manifest

        contributes = [s for s in m.provides if s in gap_slots]
        if not contributes:
            continue

        # requires 충족 판정
        #
        # board.has()만 보면 안 된다. column_semantics처럼 대상별로 채워지는 슬롯은
        # 컬럼 1개만 해석돼도 has()가 True가 되어, 5개 중 2개만 끝난 상태에서
        # 최종 합성 skill이 실행되고 목표 달성으로 종료되는 문제가 실제로 발생했다.
        # strict 모드에서는 "그 슬롯에 남은 gap이 없을 것"을 요구한다.
        if strict:
            unmet = [r for r in m.requires if r in incomplete]
        else:
            # 채울 방법이 아예 없는 슬롯은 requires에서 면제한다.
            # 그렇지 않으면 선행 skill이 blocked된 순간 후속 skill 전체가 영구 정지한다.
            skip = unreachable or set()
            unmet = [r for r in m.requires if not board.has(r) and r not in skip]
        if unmet:
            continue

        targets = [""]
        if m.per_column:
            targets = []
            for slot in contributes:
                targets.extend(gap_targets.get(slot, []))
            targets = list(dict.fromkeys(targets))
            if not targets:
                continue

        keyed_only = m.per_column and all(s in KEYED_SLOTS for s in m.provides)

        for target in targets:
            if f"{m.name}::{target}" in blocked_set:
                continue
            fact = next((f for f in facts if f.name == target), None) if target else None
            ok, why = applies(m.applies_when, fact, facts, rows)
            if not ok:
                continue

            score = 1.0 * len(contributes)
            reasons = [f"gap 기여: {[s.value for s in contributes]}", why]
            if m.applies_when.column_kinds and fact is not None:
                score += 0.4  # 범용 skill보다 컬럼 종류 특화 skill을 우선
                reasons.append(f"kind 특화({fact.kind.value})")
            if m.applies_when.always:
                score -= 0.2  # 범용 fallback은 후순위
            if m.role == SkillRole.OBSERVER:
                score += 0.6
                reasons.append("evidence 우선")
            elif m.role == SkillRole.DELIBERATOR:
                score += 0.2
                reasons.append("analysis plan")
            elif m.role == SkillRole.SYNTHESIZER:
                score -= 0.1
            score -= COST_WEIGHT.get(m.cost, 0.25)

            out.append(
                Candidate(
                    skill=m.name,
                    target=target,
                    score=round(score, 3),
                    cost=m.cost,
                    provides=list(m.provides),
                    keyed_only=keyed_only,
                    reasons=reasons,
                )
            )

    out.sort(key=lambda c: (-c.score, c.skill, c.target))
    return out


def unreachable_slots(
    registry: SkillRegistry, board: Board, gaps: List[Gap], blocked: List[str]
) -> set:
    """
    어떤 skill로도 더 이상 채울 수 없는 슬롯.

    그 슬롯을 제공하는 skill이 전부 blocked면 영원히 비어 있게 된다.
    이걸 감지하지 못하면 후속 skill이 requires를 영원히 만족하지 못해
    산출물 없이 no_applicable_skill로 끝난다 (넓은 테이블에서 실제로 발생했다).
    """
    blocked_set = set(blocked)
    out = set()
    for g in gaps:
        if board.has(g.slot):
            continue
        alive = False
        for skill in registry.all():
            if g.slot not in skill.manifest.provides:
                continue
            for t in (g.targets or [""]):
                if f"{skill.manifest.name}::{t}" not in blocked_set:
                    alive = True
                    break
            if alive:
                break
        if not alive:
            out.add(g.slot)
    return out


# ---------------------------------------------------------------------------
# 4. 배치 구성
# ---------------------------------------------------------------------------


def _writes(c: Candidate) -> set:
    """
    이 작업이 board에 쓰는 좌표 집합.

    keyed 슬롯은 (슬롯, 대상)까지 내려가야 한다. 컬럼이 다르면 같은 슬롯에 써도
    충돌하지 않기 때문이다. 단일 값 슬롯은 (슬롯, "")이라 같은 슬롯을 쓰는 두
    작업이 자동으로 배타가 된다.
    """
    return {(s, c.target if s in KEYED_SLOTS else "") for s in c.provides}


def build_batch(
    candidates: List[Candidate], registry: SkillRegistry, limit: int = MAX_BATCH
) -> List[Candidate]:
    """
    쓰기 좌표가 겹치지 않고 서로의 산출물을 필요로 하지 않는 작업을 묶는다.

    초기 버전은 per_column 작업만 묶었는데, 그러면 dependency-check /
    pii-detection / glossary-align처럼 서로 완전히 독립인 테이블 수준 skill이
    한 번에 하나씩만 돌아 파도가 불필요하게 늘어난다. 충돌 여부로 판정하면
    같은 규칙이 두 경우를 모두 덮는다.
    """
    if not candidates:
        return []

    batch: List[Candidate] = []
    taken: set = set()
    provided_slots: set = set()

    for c in candidates:
        w = _writes(c)
        if w & taken:
            continue
        # 배치 안에서 A의 산출물을 B가 선행 조건으로 요구하면 순서가 필요하다.
        requires = set(registry.get(c.skill).manifest.requires)
        if requires & provided_slots:
            continue
        if any(s in {x for x, _ in w} for s in _required_by_batch(batch, registry)):
            continue

        batch.append(c)
        taken |= w
        provided_slots |= set(c.provides)
        if len(batch) >= limit:
            break

    return batch or [candidates[0]]


def _required_by_batch(batch: List[Candidate], registry: SkillRegistry) -> set:
    out: set = set()
    for c in batch:
        out |= set(registry.get(c.skill).manifest.requires)
    return out


# ---------------------------------------------------------------------------
# 5. 결정
# ---------------------------------------------------------------------------


def decide(state: AgentState, registry: SkillRegistry, batch_limit: int = MAX_BATCH) -> Decision:
    from .state import load_board, load_facts

    board = load_board(state)
    facts = load_facts(state)
    goal_slots = state.get("goal_slots") or []

    if all(board.has(Slot(g)) for g in goal_slots):
        return Decision(done=True, stop_reason="goal_reached", note="목표 슬롯이 모두 충족됨")

    if state.get("iteration", 0) >= state.get("max_iterations", 24):
        return Decision(done=True, stop_reason="iteration_budget_exhausted", note="반복 예산 소진")

    budget = state.get("budget") or {}
    if budget.get("llm_calls", 0) >= budget.get("max_llm_calls", 10**9):
        return Decision(
            done=True,
            stop_reason="llm_budget_exhausted",
            note=f"LLM 호출 예산 소진 ({budget.get('llm_calls')}회)",
        )

    gaps = compute_gaps(board, facts, goal_slots, state.get("requested_slots") or [])
    rows = (
        int((board.get(Slot.TABLE_PROFILE) or {}).get("row_count", 0))
        if board.has(Slot.TABLE_PROFILE)
        else 0
    )
    blocked = state.get("blocked") or []

    candidates = score_candidates(registry, board, facts, gaps, blocked, rows, strict=True)
    relaxed = False

    # 완결을 영원히 기다릴 수는 없다. 컬럼 하나가 반복 실패해 blocked되면
    # 그 슬롯은 절대 완결되지 않으므로, 후보가 고갈되면 부분 충족을 허용한다.
    if not candidates:
        candidates = score_candidates(registry, board, facts, gaps, blocked, rows, strict=False)
        relaxed = bool(candidates)

    # 3차: 도달 불가능한 선행 슬롯은 면제한다.
    unreachable: set = set()
    if not candidates:
        unreachable = unreachable_slots(registry, board, gaps, blocked)
        if unreachable:
            candidates = score_candidates(
                registry, board, facts, gaps, blocked, rows, strict=False, unreachable=unreachable
            )
            relaxed = bool(candidates)

    if not candidates:
        open_gaps = [g.slot.value for g in gaps if g.is_open]
        blocked_gaps = [g.slot.value for g in gaps if not g.is_open]
        return Decision(
            done=True,
            stop_reason="no_applicable_skill",
            note=f"남은 gap={open_gaps}, 선행 미충족={blocked_gaps}. 적용 가능한 skill이 없음",
        )

    batch = build_batch(candidates, registry, batch_limit)
    suffix = " [부분 충족 상태로 진행]" if relaxed else ""
    if unreachable:
        suffix += f" [도달 불가 슬롯 면제: {sorted(s.value for s in unreachable)}]"
    if len(batch) > 1:
        names = ", ".join(f"{c.skill}::{c.target}" for c in batch)
        note = f"gap {len(gaps)}개 → 독립 작업 {len(batch)}건 병렬 실행: {names}{suffix}"
    else:
        c = batch[0]
        note = f"gap {len(gaps)}개 중 '{c.skill}' 선택 (score={c.score}, {'; '.join(c.reasons)}){suffix}"

    return Decision(
        batch=batch, note=note, considered=candidates[:6], needs_tiebreak=needs_tiebreak(candidates)
    )


# ---------------------------------------------------------------------------
# 6. LLM tie-break (그래프 미연결. CLAUDE.md TODO 참조)
# ---------------------------------------------------------------------------


class SkillChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    reason: str


def needs_tiebreak(candidates: List[Candidate], epsilon: float = 0.05) -> bool:
    return (
        len(candidates) >= 2
        and not candidates[0].keyed_only
        and abs(candidates[0].score - candidates[1].score) < epsilon
    )


def build_tiebreak_messages(state: AgentState, registry: SkillRegistry, candidates: List[Candidate]):
    listing = "\n".join(
        f"- {c.skill} (대상={c.target or '테이블 전체'}): {registry.get(c.skill).manifest.description}"
        for c in candidates
    )
    board = state.get("board") or {}
    return [
        {
            "role": "system",
            "content": "너는 데이터 자산 해석 에이전트의 플래너다. 후보 중 지금 실행할 skill 하나를 고른다. "
            "JSON object 하나만 출력한다.",
        },
        {
            "role": "user",
            "content": (
                f"[테이블]\n{state.get('asset_name','')}\n"
                f"{state.get('source_description') or '설명 없음'}\n\n"
                f"[채워진 슬롯]\n{list(board.get('values', {}).keys())}\n\n"
                f"[후보]\n{listing}\n\n"
                "가장 먼저 실행해야 할 skill의 name을 고르고 이유를 한 문장으로 답하라."
            ),
        },
    ]
