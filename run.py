#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""진입점. 구현은 src/column_semantics 아래에 있다.

    python run.py ./data.csv --output result.json

이 파일이 얇은 이유: k8s Job(k8s/column-poc-job.yaml)과 PVC 업로드
스크립트가 "레포 루트의 run.py"를 실행 지점으로 잡고 있어서, 경로 계약을
유지하면서 내부 구조만 바꿀 수 있게 남겨둔 shim이다. PYTHONPATH 설정 없이도
돌도록 여기서 src/를 sys.path에 넣는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from column_semantics.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
