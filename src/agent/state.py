"""
Blackboard 상태.

에이전트의 "현재 어디까지 처리했는지"가 전부 여기에 있다.
플래너는 이 객체의 gaps()만 보고 다음 skill을 고른다.

state에 담는 것과 담지 않는 것
  담는다   : 채워진 슬롯, 실행 이력, 실패 이력, 남은 예산
  담지 않는다 : DataFrame(직렬화 불가), SKILL.md 본문(필요할 때 로드)
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .contract import ColumnKind, Contribution, Slot


def _merge_dict(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """
    budget 같은 누적 dict용 reducer.

    덮어쓰기(기본 동작)를 쓰면 executor가 반환하는 부분 dict가 max_llm_calls를
    지워버린다. 병합 reducer로 두어 노드가 자기가 아는 키만 갱신하게 한다.
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class ColumnFact(BaseModel):
    """프로파일러가 만든 컬럼 사실. 가드레일의 근거 집합이기도 하다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ColumnKind = ColumnKind.UNKNOWN
    dtype: str = ""
    distinct_count: int = 0
    distinct_ratio: float = 0.0
    null_ratio: float = 0.0
    avg_char_length: float = 0.0
    min_value: str = ""
    max_value: str = ""
    samples: List[str] = Field(default_factory=list)


class Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    target: str = ""
    ok: bool = True
    reason: str = ""
    iteration: int = 0


class Board(BaseModel):
    """
    슬롯 저장소.

    per_column 슬롯은 dict[column -> value], 나머지는 단일 값.
    이 비대칭이 gap 계산의 핵심이다. COLUMN_SEMANTICS는
    "채워졌다/안 채워졌다"가 아니라 "어느 컬럼이 남았다"로 답해야 한다.
    """

    model_config = ConfigDict(extra="forbid")

    values: Dict[str, Any] = Field(default_factory=dict)
    keyed: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    evidence: Dict[str, List[str]] = Field(default_factory=dict)

    def put(self, c: Contribution) -> None:
        if c.key:
            self.keyed.setdefault(c.slot.value, {})[c.key] = c.value
        else:
            self.values[c.slot.value] = c.value
        if c.evidence:
            self.evidence.setdefault(c.slot.value, []).extend(c.evidence)

    def has(self, slot: Slot) -> bool:
        return slot.value in self.values or bool(self.keyed.get(slot.value))

    def get(self, slot: Slot, default: Any = None) -> Any:
        if slot.value in self.values:
            return self.values[slot.value]
        return self.keyed.get(slot.value, default)

    def keys_of(self, slot: Slot) -> List[str]:
        return list(self.keyed.get(slot.value, {}).keys())


class Gap(BaseModel):
    """아직 채워지지 않은 슬롯. targets가 있으면 대상별로 나뉜다."""

    model_config = ConfigDict(extra="forbid")

    slot: Slot
    targets: List[str] = Field(default_factory=list)
    blocked_by: List[Slot] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return not self.blocked_by


class AgentState(TypedDict, total=False):
    # 입력
    asset_id: str
    asset_name: str
    source_description: str
    # 스키마/데이터 사전에서 온 컬럼별 기존 설명. 있으면 근거 집합에 포함되어
    # 가드가 이 텍스트에 등장한 단위·수치를 허용한다.
    column_descriptions: Dict[str, str]
    data_ref: str
    language: str
    goal_slots: List[str]  # 이 슬롯들이 채워지면 종료

    # blackboard (모든 산출물의 단일 저장소)
    board: Dict[str, Any]

    # 진행 상황
    iteration: int
    max_iterations: int
    budget: Annotated[Dict[str, Any], _merge_dict]  # llm_calls, probe_runs, max_*
    requested_slots: List[str]  # skill이 실행 중 요청한 슬롯 (SkillResult.requests)
    history: Annotated[List[Dict[str, Any]], operator.add]
    blocked: List[str]  # "skill::target" 형태. 반복 실패한 조합
    plan_note: str  # 마지막 플래닝 판단 근거 (관찰성)
    batch: List[Dict[str, Any]]  # 이번 파도에 실행할 (skill, target) 목록
    elapsed_ms: int
    done: bool
    stop_reason: str


DEFAULT_GOAL_SLOTS = [Slot.ASSET_CONTEXT.value]


def new_state(
    asset_id: str,
    asset_name: str,
    data_ref: str,
    source_description: str = "",
    column_descriptions: Dict[str, str] | None = None,
    language: str = "ko",
    max_iterations: int = 24,
    max_llm_calls: int = 200,
    goal_slots: List[str] | None = None,
) -> AgentState:
    return AgentState(
        asset_id=asset_id,
        asset_name=asset_name,
        source_description=source_description,
        column_descriptions=column_descriptions or {},
        data_ref=data_ref,
        language=language,
        goal_slots=goal_slots or list(DEFAULT_GOAL_SLOTS),
        board=Board().model_dump(),
        iteration=0,
        max_iterations=max_iterations,
        budget={"llm_calls": 0, "probe_runs": 0, "max_llm_calls": max_llm_calls},
        requested_slots=[],
        history=[],
        blocked=[],
        plan_note="",
        batch=[],
        done=False,
        stop_reason="",
    )


def load_board(state: AgentState) -> Board:
    return Board.model_validate(state.get("board") or {})


def load_facts(state: AgentState) -> List[ColumnFact]:
    """
    컬럼 사실은 board의 TABLE_PROFILE 슬롯에서 파생시킨다.

    state에 column_facts를 따로 두면 board와 두 벌이 되어 반드시 어긋난다.
    실제로 초기 구현에서 profile 스킬이 board만 채우고 column_facts는 비어 있어
    gap 계산이 전부 실패했다. 저장 위치는 하나여야 한다.
    """
    board = load_board(state)
    profile = board.get(Slot.TABLE_PROFILE) or {}
    return [ColumnFact.model_validate(c) for c in profile.get("columns", [])]
