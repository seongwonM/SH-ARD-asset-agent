"""컬럼 이름을 토큰으로 쪼갠다. LLM이 약어를 풀 때 쓰는 힌트."""

from __future__ import annotations

import re
from typing import List


def split_tokens(name: str) -> List[str]:
    # snake/kebab/space + camelCase + letter/number boundaries
    x = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    x = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", x)
    x = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", x)
    return [t.lower() for t in re.split(r"[_\-\s./]+", x) if t]
