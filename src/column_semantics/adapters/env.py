"""최소 .env 파서.

폐쇄망 클러스터에서 pip install이 불가능해 python-dotenv를 쓰지 않는다.
k8s에서는 secret이 envFrom으로 환경변수를 직접 주입하므로 .env 파일 자체가
없다 - 그 경우 아무 것도 하지 않는다. 이미 설정된 환경변수는 덮지 않는다.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
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
