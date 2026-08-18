"""쉼표가 값 안에 섞여 깨진 CSV를 복구하는 유틸리티.

src/agent/csv_repair.py를 그대로 옮겨왔다(column_semantic_poc는 독립 스크립트라
agent 패키지를 import하지 않으므로, 여기서는 별도 복사본으로 둔다).

CSV 값 안에 이스케이프 안 된 ","가 들어있으면, 그 값이 여러 컬럼으로 쪼개져서
행의 필드 수가 헤더보다 많아진다("초과 컬럼"). 이 모듈은 다음 가정 아래
어느 컬럼이 쪼개졌는지 자동으로 찾아 다시 합친다.

    한 행에서 문제가 된 컬럼은 정확히 하나다. 그 컬럼 안의 ","가 N개면
    초과 컬럼 수도 N개가 되고, 문제 컬럼 뒤쪽 필드들은 원래 컬럼과 1:1로
    맞춰지므로 형식(정수/실수/날짜/문자열)이 정상 행과 같아야 한다.

정상적으로 필드 수가 맞는 행들로 컬럼별 "원래 형식"을 다수결로 추정한 뒤,
초과 컬럼이 있는 행에 대해 병합 위치 후보를 앞/뒤 필드 형식이 전부 들어맞는
지점으로 좁힌다. 후보가 정확히 하나면 그 위치를 병합해서 복구하고, 후보가
0개거나 2개 이상이면(전형적으로 문제 컬럼 두 개가 붙어있어서 구분이 안 되는
경우) 자동으로 결정할 수 없으므로 CsvRepairError를 발생시킨다.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pandas as pd

_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")


class CsvRepairError(Exception):
    """쉼표 때문에 깨진 CSV 행을 자동으로 복구하지 못했을 때 발생한다."""


def repair_ragged_csv(csv_path: str | Path, encoding: str = "utf-8") -> pd.DataFrame:
    """초과 컬럼이 있는 CSV를 읽어 값 안의 "," 때문에 쪼개진 컬럼을 복구한다.

    필드 수가 헤더보다 적은 행(값 누락 등 다른 종류의 손상)은 이 함수의
    처리 대상이 아니므로 그대로 통과시킨다.

    Args:
        csv_path: 복구할 CSV 파일 경로.
        encoding: 파일 인코딩.

    Returns:
        복구된 DataFrame.

    Raises:
        CsvRepairError: 초과 컬럼이 있는 행에서 어느 컬럼이 깨졌는지
            형식만으로 특정할 수 없을 때(후보 0개 또는 2개 이상).
    """

    with open(csv_path, encoding=encoding, newline="") as f:
        rows = list(csv.reader(f))

    if not rows:
        return pd.DataFrame()

    header, *data_rows = rows
    expected = len(header)

    clean_rows = [row for row in data_rows if len(row) == expected]
    column_types = _infer_column_types(clean_rows, expected)

    repaired_rows = []
    for row_number, row in enumerate(data_rows, start=2):  # 헤더가 1행이므로 데이터는 2행부터
        if len(row) <= expected:
            repaired_rows.append(row)
            continue
        repaired_rows.append(_repair_row(row, expected, column_types, row_number))

    # repaired_rows는 csv.reader가 나눈 문자열 그대로라, 여기서 바로 DataFrame을
    # 만들면 전부 object dtype이 되어 pd.read_csv가 원래 해주던 숫자/날짜 dtype
    # 추론을 잃어버린다(프로파일링 파이프라인이 dtype에 의존). 합쳐진 값 안에
    # 남은 ","는 csv.writer가 다시 따옴표로 감싸주므로, 텍스트로 왕복시켜
    # pd.read_csv에게 dtype 추론을 그대로 맡긴다.
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(repaired_rows)
    buffer.seek(0)
    return pd.read_csv(buffer)


def _classify_value(value: str) -> str:
    v = value.strip()
    if v == "":
        return "empty"
    if _FLOAT_RE.fullmatch(v):
        return "float"
    if _INT_RE.fullmatch(v):
        return "int"
    if _DATETIME_RE.fullmatch(v):
        return "datetime"
    return "string"


def _infer_column_types(clean_rows: list[list[str]], expected: int) -> list[str]:
    types: list[str] = []
    for col_index in range(expected):
        counts: dict[str, int] = {}
        for row in clean_rows:
            cell_type = _classify_value(row[col_index])
            if cell_type == "empty":
                continue
            counts[cell_type] = counts.get(cell_type, 0) + 1
        types.append(max(counts, key=counts.get) if counts else "empty")
    return types


def _row_matches_column_types(row: list[str], column_types: list[str]) -> bool:
    for value, expected_type in zip(row, column_types):
        # "empty"는 형식을 알 수 없다는 뜻이라 어느 쪽이든 와일드카드로 취급한다.
        if expected_type == "empty" or _classify_value(value) == "empty":
            continue
        if _classify_value(value) != expected_type:
            return False
    return True


def _repair_row(
    row: list[str], expected: int, column_types: list[str], row_number: int
) -> list[str]:
    excess = len(row) - expected

    def merge_at(i: int) -> list[str]:
        merged_value = ",".join(row[i : i + excess + 1])
        return row[:i] + [merged_value] + row[i + excess + 1 :]

    candidates = [i for i in range(expected) if _row_matches_column_types(merge_at(i), column_types)]

    if len(candidates) == 1:
        return merge_at(candidates[0])

    if not candidates:
        raise CsvRepairError(
            f"{row_number}행: 쉼표가 섞여 들어간 컬럼을 찾지 못했습니다 "
            f"(예상 컬럼 수 {expected}, 실제 필드 수 {len(row)}). 원본 값: {row}"
        )

    raise CsvRepairError(
        f"{row_number}행: 쉼표가 섞여 들어간 컬럼 후보가 {len(candidates)}개(컬럼 인덱스 {candidates})라 "
        "하나로 특정할 수 없습니다. 서로 붙어있는 컬럼 두 개에 모두 쉼표가 들어있어 "
        f"자동 복구가 불가능한 경우로 보입니다. 수동으로 확인해 주세요. 원본 값: {row}"
    )
