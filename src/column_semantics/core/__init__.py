"""결정론적 계산 계층. DataFrame을 받아 사실(evidence)을 만든다.

이 패키지는 adapters/pipeline을 import하지 않는다. LLM 없이 단독으로 테스트된다.
"""

from column_semantics.core.evidence import build_table_evidence
from column_semantics.core.jsonx import clean_for_json, json_safe
from column_semantics.core.naming import split_tokens
from column_semantics.core.probes import (
    ProbeExpressionError,
    apply_probes,
    eval_probe_expression,
    run_probe,
)
from column_semantics.core.profiling import profile_columns
from column_semantics.core.relations import (
    build_relation_groups,
    find_grain_candidates,
    relation_evidence,
)

__all__ = [
    "ProbeExpressionError",
    "apply_probes",
    "build_relation_groups",
    "build_table_evidence",
    "clean_for_json",
    "eval_probe_expression",
    "find_grain_candidates",
    "json_safe",
    "profile_columns",
    "relation_evidence",
    "run_probe",
    "split_tokens",
]
