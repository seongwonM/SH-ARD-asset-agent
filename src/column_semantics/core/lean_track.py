"""단계마다 따로 받아둔 **최소 출력**의 기록.

운영에서 실제로 쓰는 것은 컬럼 의미와 테이블 의미 두 축인데, 지금 단계들의 출력에는
분석하려고 붙인 필드가 훨씬 많다(후보 목록, 근거/반대근거, confidence, 대안 타입…).
그걸 빼도 의미가 그대로 나오는지 알아야 프롬프트를 줄일 수 있고, 알려면 **같은
입력으로 나란히 받아 두는 수밖에 없다.**

그래서 각 고정 단계는 자기 payload로 최소 출력 프롬프트를 한 번 더 부르고, 그
결과가 여기 쌓인다. 쌓이기만 한다 - **파이프라인은 이 값을 읽지 않는다.** 읽는
순간 "최소 출력으로 돌린 파이프라인"이 되어 버려서 비교 대상이 사라진다.

호출 실패도 남긴다. 최소 출력이 자꾸 실패한다는 것 자체가 그 프롬프트에 대한
결과이고, 조용히 비면 "안 돌았다"와 "돌았는데 못 냈다"가 구분되지 않는다.

Timeline/LLMLog와 같은 이유로 락을 걸고(컬럼별 병렬 호출이 동시에 쓴다), 같은
이유로 core에 있다 - 파일도 LLM도 모르는 순수 기록이다.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from column_semantics.core.clock import now_iso


class LeanTrack:
    def __init__(self) -> None:
        self._items: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(
        self,
        stage: str,
        target: str,
        output: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        **fields: Any,
    ) -> None:
        entry = {
            "stage": stage,
            # 무엇에 대한 출력인가. 컬럼 단위면 컬럼 이름, 그룹 단위면 그룹 이름,
            # 테이블 단위면 "table"이다. 짝을 맞춰 볼 때 이 키로 붙인다.
            "target": target,
            "output": output,
            "error": error,
            "at": now_iso(),
            **fields,
        }
        with self._lock:
            self._items.append(entry)

    def entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._items)

    def by_stage(self) -> Dict[str, Dict[str, Any]]:
        """{단계: {대상: 출력}}. 같은 대상이 여러 번 나오면(수정 라운드) 마지막이 남는다."""
        out: Dict[str, Dict[str, Any]] = {}
        for entry in self.entries():
            slot = out.setdefault(entry["stage"], {})
            slot[entry["target"]] = entry["output"] if entry["error"] is None else {
                "error": entry["error"]
            }
        return out


__all__ = ["LeanTrack"]
