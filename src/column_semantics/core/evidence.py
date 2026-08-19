"""테이블 하나의 증거 묶음. LLM이 보는 입력은 전부 여기서 나온다."""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from column_semantics.core.jsonx import clean_for_json
from column_semantics.core.profiling import profile_columns
from column_semantics.core.relations import find_grain_candidates, relation_evidence


def build_table_evidence(df: pd.DataFrame) -> Dict[str, Any]:
    profiles = profile_columns(df)
    rel = relation_evidence(df, profiles)
    grain = find_grain_candidates(df)

    return clean_for_json(
        {
            "table": {
                "row_count": int(len(df)),
                "column_count": int(len(df.columns)),
                "columns": [str(c) for c in df.columns],
            },
            "column_profiles": profiles,
            "relation_evidence": rel,
            "grain_candidates": grain,
        }
    )
