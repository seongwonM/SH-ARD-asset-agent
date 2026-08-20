"""컬럼 하나가 단계를 지나며 어떻게 바뀌었는지에 대한 기록.

파이프라인은 `column_interpretation.columns[col]`을 제자리에서 덮어쓴다 - gap
보충이 `selected_meaning`을 갈아끼우고, 수정 라운드가 해석을 통째로 다시 쓴다.
최종 결과만 보면 "원래 뭐였는데 무엇 때문에 바뀌었는지"가 사라진다. 그 소실을
막는 게 이 객체의 전부다.

Timeline과 같은 이유로 락을 건다(컬럼별 병렬 호출이 동시에 append한다).
그리고 Timeline과 같은 이유로 core에 있다 - 파일도 LLM도 모르는 순수 기록이다.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional

from column_semantics.core.clock import now_iso


def changed_fields(before: Any, after: Any) -> List[str]:
    """두 스냅샷에서 값이 실제로 달라진 필드 이름만 돌려준다.

    dict가 아니면 비교할 필드가 없으므로 빈 리스트다("바뀌지 않았다"가 아니라
    "필드 단위로 말할 수 없다"는 뜻이고, 그 경우 before/after 원본이 남는다).
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


class ColumnHistory:
    def __init__(self) -> None:
        self._stages: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def record(self, column: str, stage: str, value: Any = None, **fields: Any) -> None:
        """그 단계에서 나온 값을 그대로 남긴다(이전 값이 없는 최초 산출)."""
        self._append(column, {"stage": stage, "value": value, **fields})

    def record_change(
        self,
        column: str,
        stage: str,
        before: Any,
        after: Any,
        **fields: Any,
    ) -> None:
        """덮어쓰기를 before/after로 남긴다.

        `changed`가 빈 리스트면 "그 단계가 돌았지만 값은 그대로"라는 뜻이다 -
        이것도 기록할 가치가 있는 사실이라 항목 자체를 빼지 않는다.
        """
        self._append(
            column,
            {
                "stage": stage,
                "before": before,
                "after": after,
                "changed": changed_fields(before, after),
                **fields,
            },
        )

    def _append(self, column: str, entry: Dict[str, Any]) -> None:
        # 기록은 반드시 스냅샷이다. 호출부가 넘긴 dict는 파이프라인이 계속 제자리에서
        # 고쳐 쓰는 바로 그 객체라, 참조를 그대로 들고 있으면 나중 변경이 과거
        # 단계에까지 소급돼 "1차 해석이 무엇이었는지"가 사라진다.
        entry = copy.deepcopy(entry)
        entry["at"] = now_iso()
        with self._lock:
            self._stages.setdefault(column, []).append(entry)

    def stages(self, column: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._stages.get(column, []))

    def columns(self, order: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """{컬럼: 단계 목록}. order를 주면 그 순서(원본 컬럼 순서)로 정렬한다."""
        with self._lock:
            snapshot = {col: list(entries) for col, entries in self._stages.items()}
        if order is None:
            return snapshot
        ordered = {col: snapshot[col] for col in order if col in snapshot}
        ordered.update({col: v for col, v in snapshot.items() if col not in ordered})
        return ordered
