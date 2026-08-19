"""타임스탬프. KST(UTC+9) 고정 오프셋만 쓴다.

zoneinfo/tzdata에 기대지 않는다 - 슬림 컨테이너 이미지에는 시간대 DB가 없는
경우가 많아 "Asia/Seoul" 같은 이름 기반 시간대는 조용히 무시되거나(플랫폼에
따라 다름) 에러가 난다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")
