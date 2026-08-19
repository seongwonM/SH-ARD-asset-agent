"""CSV 파일 읽기. 인코딩 추정과 깨진 행 복구까지가 이 어댑터의 책임이다.

다른 저장소(DB/객체스토리지)를 붙일 때는 여기와 같은 시그니처
(경로 -> DataFrame)의 모듈을 하나 더 만들어 파이프라인에 주입한다 -
core는 DataFrame만 알면 되므로 손댈 필요가 없다.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from column_semantics.adapters.csv_repair import CsvRepairError, repair_ragged_csv

__all__ = ["CsvRepairError", "read_csv_safely", "repair_ragged_csv"]


def read_csv_safely(path: Path) -> pd.DataFrame:
    errors = []
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=enc, low_memory=False)
        except pd.errors.ParserError as e:
            # 값 안에 이스케이프 안 된 ","가 섞여 필드 수가 헤더보다 많아진
            # 경우일 수 있다 - 인코딩을 바꿔봐야 소용없으니 복구를 시도한다.
            try:
                return repair_ragged_csv(path, encoding=enc)
            except CsvRepairError as repair_error:
                errors.append(f"{enc}: {e} (복구 시도 실패: {repair_error})")
        except Exception as e:
            errors.append(f"{enc}: {e}")
    raise RuntimeError("CSV를 읽지 못했습니다.\n" + "\n".join(errors))
