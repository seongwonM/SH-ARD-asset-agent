"""skill이 요청한 데이터 probe.

skill은 행 단위 데이터를 볼 수 없고 집계된 evidence만 본다. 그래서 컬럼 간
정확한 관계를 스스로 계산할 수 없다. skill 출력의 check에 `probe`가 붙어 있으면
(컬럼 이름들에 대한 산술/비교식, 예: "a + b" 또는 "a <= b") 그 식을 실제
DataFrame에 대고 평가해 실측값을 돌려준다.

이건 범용 계산기다. **어떤 주장을 검사할지, 무슨 관계를 볼지는 skill이 정한다.**
여기는 산술/비교식을 안전하게 평가하는 방법만 안다. skill이 물어볼 수 있는
범위를 넓히려면 skill이 쓰는 식을 넓히면 되고, 여기에 케이스를 추가하지 않는다.

probe는 반증 도구다. 통과가 참을 증명하지 않고, 실패가 거짓을 증명한다.
그리고 실행 실패(컬럼 없음/식 오류/표본 부족)는 반증이 아니다 - 그 경우
check를 fail로 떨어뜨리지 않고 손대지 않은 채로 둔다(run_probe -> None).
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

_PROBE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_PROBE_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_PROBE_COMPARE = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}
_PROBE_FUNCS = {"abs": np.abs, "min": np.minimum, "max": np.maximum, "round": np.round}

# 실측 통과율을 check.status로 옮기는 기준. 여기 말고 다른 곳에서 판정하지 않는다.
PASS_RATIO = 0.95
WARNING_RATIO = 0.7


class ProbeExpressionError(Exception):
    pass


def _eval_probe_node(node: ast.AST, variables: Dict[str, pd.Series]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_probe_node(node.body, variables)
    if isinstance(node, ast.BinOp) and type(node.op) in _PROBE_BINOPS:
        return _PROBE_BINOPS[type(node.op)](
            _eval_probe_node(node.left, variables), _eval_probe_node(node.right, variables)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _PROBE_UNARYOPS:
        return _PROBE_UNARYOPS[type(node.op)](_eval_probe_node(node.operand, variables))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _PROBE_COMPARE:
        return _PROBE_COMPARE[type(node.ops[0])](
            _eval_probe_node(node.left, variables), _eval_probe_node(node.comparators[0], variables)
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _PROBE_FUNCS
        and not node.keywords
    ):
        return _PROBE_FUNCS[node.func.id](*(_eval_probe_node(a, variables) for a in node.args))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ProbeExpressionError(f"unknown variable: {node.id}")
        return variables[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise ProbeExpressionError(f"disallowed expression: {ast.dump(node)}")


def eval_probe_expression(expression: str, variables: Dict[str, pd.Series]) -> Any:
    tree = ast.parse(expression, mode="eval")
    return _eval_probe_node(tree, variables)


def run_probe(df: pd.DataFrame, probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """probe 하나를 실제 데이터에 대고 평가한다. 평가할 수 없으면 None."""
    expression = probe.get("expression")
    columns = probe.get("columns")
    if not isinstance(expression, str) or not isinstance(columns, dict) or not columns:
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in columns.items()):
        return None
    if not all(col in df.columns for col in columns.values()):
        return None

    variables: Dict[str, pd.Series] = {}
    valid = pd.Series(True, index=df.index)
    for alias, col in columns.items():
        s = pd.to_numeric(df[col], errors="coerce")
        variables[alias] = s
        valid &= s.notna()
    if valid.sum() < 3:
        return None
    variables = {k: v[valid] for k, v in variables.items()}

    try:
        result = eval_probe_expression(expression, variables)
    except (ProbeExpressionError, SyntaxError, TypeError, ZeroDivisionError):
        return None
    if not isinstance(result, pd.Series) or len(result) == 0:
        return None

    observed: Dict[str, Any] = {"expression": expression, "columns": columns, "n": int(len(result))}

    if result.dtype == bool:
        observed["true_ratio"] = float(result.mean())
    else:
        result = result.replace([np.inf, -np.inf], np.nan).dropna()
        if len(result) == 0:
            return None
        observed["median"] = float(result.median())
        observed["min"] = float(result.min())
        observed["max"] = float(result.max())
        target = probe.get("target")
        if isinstance(target, (int, float)):
            tolerance = probe.get("tolerance", 0.05)
            observed["target"] = float(target)
            observed["tolerance"] = float(tolerance)
            observed["within_tolerance_ratio"] = float(((result - target).abs() <= tolerance).mean())

    return observed


def apply_probes(
    df: pd.DataFrame,
    validation: Dict[str, Any],
    probe_log: List[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """semantic_validation 결과의 check들을 실측에 대고 판정하고 status를 갱신한다.

    실측값 자체는 check에 붙이지 않고 `probe_log`에만 쌓는다. 룰베이스로 계산한
    값이 LLM 출력 사이사이에 섞여 있으면 "무엇이 측정값이고 무엇이 주장인지"가
    흐려져서, 측정값은 한곳에 모으고 check에는 참조(`probe_id`)만 남긴다.
    """
    checks = validation.get("checks")
    if not isinstance(checks, list):
        return validation

    for check in checks:
        probe = check.get("probe") if isinstance(check, dict) else None
        if not isinstance(probe, dict):
            continue
        observed = run_probe(df, probe)
        if observed is None:
            continue

        probe_id = f"probe-{len(probe_log) + 1}"
        probe_log.append(
            {
                "probe_id": probe_id,
                **(context or {}),
                "hypothesis": check.get("hypothesis"),
                "observed": observed,
            }
        )
        check["probe_id"] = probe_id
        check["probe_verified"] = True
        ratio = observed.get("within_tolerance_ratio", observed.get("true_ratio"))
        if ratio is not None:
            if ratio >= PASS_RATIO:
                check["status"] = "pass"
            elif ratio >= WARNING_RATIO:
                check["status"] = "warning"
            else:
                check["status"] = "fail"

    if any(c.get("status") in {"warning", "fail"} for c in checks if isinstance(c, dict)):
        validation["overall_status"] = "needs_revision"
    elif checks:
        validation["overall_status"] = "pass"

    return validation


def with_measurements(
    checks: List[Dict[str, Any]], probe_log: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """재시도 힌트용으로 check에 실측값을 도로 붙인 사본을 만든다.

    저장할 때는 측정값과 주장을 갈라놓지만, LLM에게 "왜 틀렸는지" 되돌려줄 때는
    실측값이 함께 가야 한다 - 반증의 근거가 곧 다음 시도의 힌트다. skill이 쓴
    서술형 `observed`를 덮지 않도록 `measured`라는 별도 필드에 넣는다.
    """
    by_id = {r.get("probe_id"): r.get("observed") for r in probe_log}
    out: List[Dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        measured = by_id.get(check.get("probe_id"))
        out.append({**check, "measured": measured} if measured is not None else dict(check))
    return out
