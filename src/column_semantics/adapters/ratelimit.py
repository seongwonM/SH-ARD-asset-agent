"""RPM 상한(슬라이딩 60초 윈도) + 동시 실행 수 상한."""

from __future__ import annotations

import threading
import time
from typing import List


class RateLimiter:
    """컬럼별/그룹별 병렬 호출이 전부 이걸 통해서 나간다. 여러 스레드가 동시에
    acquire()해도 분당 요청 수가 requests_per_minute을 넘지 않는다."""

    def __init__(self, requests_per_minute: int, max_concurrency: int):
        self.max_concurrency = max(1, max_concurrency)
        self._rpm = max(1, requests_per_minute)
        self._lock = threading.Lock()
        self._timestamps: List[float] = []
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    def acquire(self) -> None:
        self._semaphore.acquire()
        while True:
            with self._lock:
                now = time.time()
                self._timestamps = [t for t in self._timestamps if now - t < 60]
                if len(self._timestamps) < self._rpm:
                    self._timestamps.append(now)
                    return
                wait = 60 - (now - self._timestamps[0])
            time.sleep(max(wait, 0.05))

    def release(self) -> None:
        self._semaphore.release()
