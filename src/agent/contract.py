"""
Skill 계약.

이 파일이 시스템의 유일한 확장 지점이다.
새 skill을 추가할 때 그래프나 플래너를 수정할 필요가 없어야 한다.
skills/<name>/SKILL.md 하나를 추가하면 registry가 자동으로 인덱싱한다.

설계 근거
- Anthropic Agent Skills: SKILL.md의 YAML frontmatter만 인덱스에 올리고,
  선택된 뒤에야 본문을 로드하는 progressive disclosure 구조.
  skill이 20개가 되어도 플래너의 컨텍스트는 description 20줄로 유지된다.
- Blackboard architecture(Hayes-Roth, 1985): 공유 상태에 여러 knowledge source가
  기여하고, control 컴포넌트가 "현재 상태에서 지금 기여 가능한 KS"를 매 스텝 고른다.
  고정 파이프라인이 아니라 상태 기반 선택이라는 점이 이 에이전트의 핵심.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, List, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .probes import ProbeRequest, ProbeResult


class Slot(str, Enum):
    """
    Asset Context를 구성하는 정보 슬롯.

    파이프라인 '단계'가 아니라 '채워야 할 빈칸'으로 모델링한다.
    이 차이가 중요하다. 단계는 순서를 강제하지만, 슬롯은
    "무엇이 비었는가"만 말하므로 플래너가 순서를 자유롭게 정할 수 있다.
    """

    TABLE_PROFILE = "table_profile"  # 컬럼 구조/통계 사실
    COLUMN_SEMANTICS = "column_semantics"  # 컬럼별 의미 (부분 충족 가능)
    GRAIN = "grain"  # 한 행이 무엇을 의미하는가
    LINKAGE = "linkage"  # 다른 자산과 이어질 수 있는 키
    CONSTRAINTS = "constraints"  # 컬럼 간 함수 종속 등 데이터 정합성 사실
    COMPLIANCE = "compliance"  # PII 등 규제 대상 컬럼 태깅
    GLOSSARY = "glossary"  # 표준용어(business glossary) 정렬
    TOPIC = "topic"
    SUMMARY = "summary"
    SEARCH_TERMS = "search_terms"
    VERIFICATION = "verification"  # 주장별 probe 검증 결과
    ASSET_CONTEXT = "asset_context"  # 최종 산출물


class ColumnKind(str, Enum):
    """skill의 적용 조건에 쓰이는 컬럼 분류. 프로파일러가 결정론적으로 부여한다."""

    IDENTIFIER = "identifier"
    TEMPORAL = "temporal"
    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    FREE_TEXT = "free_text"
    SPATIAL = "spatial"
    UNKNOWN = "unknown"


class Cost(str, Enum):
    FREE = "free"  # LLM 호출 없음
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


COST_WEIGHT = {Cost.FREE: 0.0, Cost.LOW: 0.1, Cost.MEDIUM: 0.25, Cost.HIGH: 0.5}


# ---------------------------------------------------------------------------
# SKILL.md frontmatter 스키마
# ---------------------------------------------------------------------------


class AppliesWhen(BaseModel):
    """
    적용 조건. 전부 선택적이고, 지정된 항목만 AND로 평가한다.
    조건을 코드가 아니라 선언으로 두는 이유: skill 추가 시 파이썬을 안 건드리게 하려고.
    """

    model_config = ConfigDict(extra="forbid")

    column_kinds: List[ColumnKind] = Field(default_factory=list)
    min_columns: int = 0
    min_rows: int = 0
    column_name_patterns: List[str] = Field(default_factory=list)
    always: bool = False


class SkillManifest(BaseModel):
    """SKILL.md의 YAML frontmatter."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(description="플래너가 보는 유일한 설명. 언제 쓰는지가 드러나야 한다.")
    requires: List[Slot] = Field(default_factory=list)
    provides: List[Slot] = Field(default_factory=list)
    applies_when: AppliesWhen = Field(default_factory=AppliesWhen)
    cost: Cost = Cost.MEDIUM
    per_column: bool = False  # True면 컬럼 단위로 반복 실행되는 skill
    max_attempts: int = 3
    version: str = "1"


# ---------------------------------------------------------------------------
# 실행 컨텍스트 / 결과
# ---------------------------------------------------------------------------


class SkillContext(BaseModel):
    """handler가 받는 입력. state 전체가 아니라 필요한 것만 잘라서 전달한다."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    asset_id: str
    asset_name: str
    source_description: str = ""
    language: str = "ko"
    data_ref: str = ""

    instructions: str = ""  # SKILL.md 본문 (progressive disclosure로 여기서 처음 로드됨)
    target: str = ""  # per_column skill이면 컬럼명
    # 스키마/데이터 사전의 기존 컬럼 설명.
    # column_description은 대상 컬럼 것, column_descriptions는 전체(테이블 수준 skill용).
    column_description: str = ""
    column_descriptions: Dict[str, str] = Field(default_factory=dict)
    board: Dict[str, Any] = Field(default_factory=dict)  # 지금까지 채워진 슬롯
    repair_hints: List[str] = Field(default_factory=list)


class Contribution(BaseModel):
    """skill이 blackboard에 올리는 기여 1건."""

    model_config = ConfigDict(extra="forbid")

    slot: Slot
    key: str = ""  # per_column이면 컬럼명, 아니면 빈 문자열
    value: Any = None
    evidence: List[str] = Field(default_factory=list)
    confidence: float = 0.0


class VerifiableClaim(BaseModel):
    """
    검사 가능한 주장 + 그것을 반증할 probe.

    skill이 "이 컬럼은 primary key다" 같은 데이터로 확인 가능한 주장을 하면,
    반드시 그 주장을 깨뜨릴 수 있는 probe를 함께 낸다.
    executor가 자동 실행하고, 실패하면 probe의 실측값이 재시도 힌트가 된다.

    이 구조가 없으면 LLM이 자기 주장을 자기가 검증하는 순환에 빠진다.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str
    probe: ProbeRequest
    critical: bool = True  # False면 실패해도 경고만 남기고 통과


class SkillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    ok: bool = True
    contributions: List[Contribution] = Field(default_factory=list)
    claims: List[VerifiableClaim] = Field(default_factory=list)
    notes: str = ""
    # skill이 스스로 "이건 내가 못 하겠고 저 슬롯이 먼저 필요하다"고 신호할 수 있다.
    # 플래너가 놓친 의존성을 실행 중에 발견하는 경로.
    requests: List[Slot] = Field(default_factory=list)


class SkillHandler(Protocol):
    """skills/<name>/handler.py 가 노출해야 하는 인터페이스."""

    async def __call__(self, ctx: SkillContext, deps: "SkillDeps") -> SkillResult: ...


class SkillDeps(ABC):
    """handler가 쓰는 외부 자원. 테스트에서 mock으로 갈아끼운다."""

    @abstractmethod
    async def structured(self, messages: List[Dict[str, str]], model_cls: type, stage: str): ...

    @abstractmethod
    def dataframe(self, ref: str): ...

    def probe(self, ref: str, req: ProbeRequest) -> ProbeResult:
        """
        handler가 프롬프트를 만들기 *전에* 데이터를 확인할 때 쓴다.
        LLM에게 추측시키는 대신 사실을 먼저 주는 편이 항상 낫다.
        """
        from .probes import run_probe

        return run_probe(req, self.dataframe(ref))
