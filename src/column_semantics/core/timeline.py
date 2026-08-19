"""실행 궤적 기록. 결과 JSON의 `timeline` 블록이 이 객체의 내용 그대로다.

여러 스레드(컬럼별/그룹별 병렬 호출)가 동시에 append하므로 락을 건다.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from column_semantics.core.clock import now_iso


class Timeline:
    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, **fields: Any) -> None:
        with self._lock:
            self._events.append(fields)

    @contextmanager
    def measure(self, **fields: Any) -> Iterator[Dict[str, Any]]:
        """블록 실행 시간을 재서 started_at/finished_at/elapsed_seconds와 함께 남긴다.

        yield된 dict에 필드를 더 넣으면 그대로 기록된다(실행 후에야 알 수 있는
        값 - 처리 건수 등 - 을 붙이는 용도).
        """
        started_at = now_iso()
        started = time.time()
        extra: Dict[str, Any] = {}
        try:
            yield extra
        finally:
            self.add(
                **fields,
                **extra,
                started_at=started_at,
                finished_at=now_iso(),
                elapsed_seconds=round(time.time() - started, 3),
            )

    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)
