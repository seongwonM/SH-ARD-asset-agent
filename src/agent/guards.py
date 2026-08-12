"""
가드레일.

structured output은 형식만 보장한다. 의미는 보장하지 않는다.
`{"unit": "kPa"}`는 스키마상 완벽히 유효하지만, 입력 어디에도 kPa가 없으면
hallucination이다. 이 계층이 없으면 형식 강제는 오히려
"그럴듯한 오류를 안정적으로 생산하는 장치"가 된다.

검사 대상은 SkillResult.contributions 안의 '자연어 값'으로 한정한다.
모든 문자열을 무차별로 검사하면 제어용 라벨(예: evidence="column_name")을
오탐하고, 그 결과 정상 출력까지 전부 실패 처리된다.
false positive는 여기서 false negative보다 위험하다. 전부 막히면
가드를 꺼둔 것과 결과가 같아지기 때문이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .contract import SkillResult

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_HANGUL = re.compile(r"[가-힣]")
_SNAKE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

# 관계·인과 주장 금지. 자산 간 관계 추론은 이 에이전트의 책임이 아니다.
FORBIDDEN_TERMS = ("조인", "join", "외래키", "foreign key", "때문에", "원인으로", "정규화")

# contributions의 value dict에서 자연어로 간주할 키
NL_KEYS = ("summary", "description", "meaning", "purpose", "topic", "note", "grain")


@dataclass
class GuardContext:
    language: str = "ko"
    column_names: List[str] = field(default_factory=list)
    allowed_numbers: List[str] = field(default_factory=list)
    grounding_text: str = ""


@dataclass
class Violation:
    guard: str
    message: str
    hint: str = ""


def _nl_texts(value: Any) -> List[str]:
    """자연어로 간주할 문자열만 뽑는다."""
    out: List[str] = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for k, v in value.items():
            if k in NL_KEYS:
                out.extend(_nl_texts(v))
        return out
    if isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_nl_texts(v))
    return out


def run_guards(result: SkillResult, ctx: GuardContext) -> List[Violation]:
    texts: List[str] = []
    for c in result.contributions:
        texts.extend(_nl_texts(c.value))

    violations: List[Violation] = []
    allowed_ids = {c.lower() for c in ctx.column_names} | {
        t.lower() for t in _SNAKE.findall(ctx.grounding_text)
    }

    for text in texts:
        # 1) 지어낸 컬럼명/식별자
        for token in _SNAKE.findall(text):
            if "_" in token and token.lower() not in allowed_ids:
                violations.append(
                    Violation(
                        "identifier_grounding",
                        f"입력에 없는 식별자: {token}",
                        f"실제 컬럼명 {ctx.column_names} 만 사용하라.",
                    )
                )
                break

        # 2) 근거 없는 수치 (기준값·규격 hallucination의 주 경로)
        for num in _NUM.findall(text):
            clean = num.replace(",", "")
            if clean in ctx.allowed_numbers or (clean.isdigit() and len(clean) <= 2):
                continue
            violations.append(
                Violation(
                    "numeric_grounding",
                    f"입력에서 확인되지 않는 수치: {num}",
                    "표본값, 값 범위, 원본 설명에 등장한 수치만 사용하라. 계산·추정한 값도 쓰지 않는다.",
                )
            )
            break

        # 3) 관계·인과 주장
        low = text.lower()
        for term in FORBIDDEN_TERMS:
            if term.lower() in low:
                violations.append(
                    Violation(
                        "no_relation_claim",
                        f"허용되지 않는 관계·인과 표현: '{term}'",
                        "이 테이블 안에서 확인 가능한 사실만 기술하라.",
                    )
                )
                break

    # 4) 출력 언어
    if ctx.language == "ko":
        long_texts = [t for t in texts if len(t.strip()) >= 8]
        if long_texts and not _HANGUL.search(" ".join(long_texts)):
            violations.append(
                Violation(
                    "language_consistency",
                    "한국어 출력이 요구되었으나 한글이 없음",
                    "설명 문장을 한국어로 작성하라. 컬럼명과 기술 용어는 원문 유지 가능.",
                )
            )

    return violations
