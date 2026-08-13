"""
.env 로딩과 실험 하이퍼파라미터 해석을 한 곳에 모은다.

python-dotenv 없이 직접 파싱한다(최소 의존성 원칙).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List


def load_dotenv_file(path: str = ".env") -> None:
    """값 뒤 인라인 주석까지 처리하는 최소 파서. 이미 설정된 환경변수는 덮지 않는다."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #")[0].strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


def get_models() -> List[str]:
    """LLM_MODEL1/2/3 중 설정된 것만 순서대로 모은다. 하나도 없으면 LLM_MODEL로 폴백."""
    models = [
        v for i in (1, 2, 3) if (v := os.environ.get(f"LLM_MODEL{i}", "").strip())
    ]
    if models:
        return models
    return [os.environ.get("LLM_MODEL", "gpt-4o-mini")]


def get_reps(default: int = 3) -> int:
    return int(os.environ.get("ROBUSTNESS_REPS", default))
