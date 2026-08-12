"""
오프라인 스텁 deps.

LLM 호출 없이 파이프라인 전 구간을 돌린다. 용도는 둘이다.
  1. 하네스 자체의 오버헤드 측정 (probe/플래너/직렬화가 얼마나 먹는지)
  2. API 비용 0으로 배치 로직·격리·재시도 경로 확인

품질 측정에는 쓸 수 없다. 여기서 나오는 요약은 LLM이 만든 게 아니다.

값은 프롬프트에 실린 프로파일 사실에서 규칙으로 뽑는다. Literal 필드에
아무 값이나 넣으면 probe에 걸려 전부 blocked로 끝나므로, 최소한
"데이터와 모순되지 않는" 선택을 하도록 만들었다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Type, get_args

from pydantic import BaseModel

from agent.contract import SkillDeps

_COL = re.compile(r"^- 컬럼명:\s*(.+)$", re.M)
_KIND = re.compile(r"^- 분류:\s*(.+)$", re.M)
_RATIO = re.compile(r"비율\s*([0-9.]+)")
_LIST_ITEM = re.compile(r"^- ([A-Za-z_][A-Za-z0-9_]*)\s*[:(]", re.M)


class OfflineDeps(SkillDeps):
    def __init__(self, frames: Dict[str, Any] | None = None):
        self._frames = frames or {}
        self.calls = 0

    def register_frame(self, ref: str, frame) -> str:
        self._frames[ref] = frame
        return ref

    def dataframe(self, ref: str):
        if ref not in self._frames:
            raise KeyError(f"등록되지 않은 data_ref: {ref}")
        return self._frames[ref]

    def get_stats(self) -> Dict[str, Any]:
        return {
            "llm_call_count": self.calls,
            "llm_avg_latency_seconds": 0.0,
            "llm_prompt_tokens": 0,
            "llm_completion_tokens": 0,
            "llm_total_tokens": 0,
            "parse_failures": 0,
            "http_retries": 0,
        }

    async def structured(self, messages, model_cls: Type[BaseModel], stage: str):
        self.calls += 1
        user = messages[-1]["content"]
        return model_cls(**_fill(model_cls, user))


# ---------------------------------------------------------------------------


def _target_column(user: str) -> str:
    m = _COL.search(user)
    return m.group(1).strip() if m else ""


def _target_kind(user: str) -> str:
    m = _KIND.search(user)
    return m.group(1).strip() if m else ""


def _distinct_ratio(user: str) -> float:
    m = _RATIO.search(user)
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


def _listed_columns(user: str) -> List[str]:
    """테이블 수준 프롬프트의 '- <컬럼>: ...' 목록."""
    return list(dict.fromkeys(_LIST_ITEM.findall(user)))


def _literal_choice(annotation, prefer: List[str]) -> Any:
    """Literal 후보 중 prefer 순으로 고르고, 없으면 첫 번째."""
    options = list(get_args(annotation))
    if not options:
        return ""
    for p in prefer:
        if p in options:
            return p
    return options[0]


def _fill(model_cls: Type[BaseModel], user: str) -> Dict[str, Any]:
    col = _target_column(user)
    kind = _target_kind(user)
    ratio = _distinct_ratio(user)
    out: Dict[str, Any] = {}

    for name, field in model_cls.model_fields.items():
        ann = field.annotation
        origin_args = get_args(ann)
        is_literal = bool(origin_args) and all(isinstance(a, str) for a in origin_args)

        if name in ("column", "column_name"):
            out[name] = col
        elif name == "confidence":
            out[name] = 0.5
        elif name == "role" and is_literal:
            # 유일하지 않은데 primary라고 하면 probe가 반증한다.
            out[name] = _literal_choice(ann, ["primary"] if ratio >= 0.99 else ["reference", "business"])
        elif name == "resolution" and is_literal:
            # 더 거친 단위는 항상 안전하다.
            out[name] = _literal_choice(ann, ["Day", "Month", "Year"])
        elif name == "level" and is_literal:
            out[name] = _literal_choice(ann, ["none"])
        elif is_literal:
            out[name] = _literal_choice(ann, ["not_found", "none", "unknown"])
        elif ann is bool:
            out[name] = False
        elif ann in (int, float):
            out[name] = 0
        elif name in ("key_columns",):
            out[name] = [col] if col else []
        elif name in ("mappings", "columns"):
            out[name] = []  # 지어내지 않는다
        elif name in ("observed_values", "key_points", "use_cases", "search_terms"):
            out[name] = []
        elif getattr(ann, "__origin__", None) is list:
            out[name] = []
        elif name in ("meaning", "summary", "grain", "topic"):
            subject = col or "이 테이블"
            # 내부 kind 라벨을 문장에 넣지 않는다. 가드가 컬럼명으로 오인한다.
            out[name] = f"[offline] {subject}에 대한 스텁 설명"
        else:
            out[name] = ""
    return out
