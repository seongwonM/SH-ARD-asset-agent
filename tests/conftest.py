from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SKILL_DIR = ROOT / "skills"


@pytest.fixture
def equipment_df() -> pd.DataFrame:
    """설비 로그를 흉내낸 표본.

    - run_id: 유일 (grain 후보가 되어야 한다)
    - equipment_id: 중복 있음 (PK 주장은 반증되어야 한다)
    - power_value <= power_limit 은 **거짓** (일부러 초과값을 넣었다).
      probe가 LLM 주장을 반증하는지 확인하는 데 쓴다.
    """
    return pd.DataFrame(
        {
            "run_id": [f"R{i:03d}" for i in range(1, 13)],
            "equipment_id": ["EQ-1", "EQ-1", "EQ-2", "EQ-2", "EQ-3", "EQ-3"] * 2,
            "power_value": [10, 20, 30, 40, 55, 60, 15, 25, 70, 45, 80, 65],
            "power_limit": [50] * 12,
            "run_at": pd.date_range("2026-01-01", periods=12, freq="h").astype(str).tolist(),
            "status_code": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
        }
    )


@pytest.fixture
def equipment_csv(tmp_path: Path, equipment_df: pd.DataFrame) -> Path:
    path = tmp_path / "equipment_log.csv"
    equipment_df.to_csv(path, index=False)
    return path
