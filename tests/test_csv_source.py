from __future__ import annotations

import pandas as pd
import pytest

from column_semantics.adapters.csv_source import CsvRepairError, read_csv_safely


def test_reads_plain_csv(equipment_csv):
    df = read_csv_safely(equipment_csv)
    assert len(df) == 12
    assert "power_value" in df.columns


def test_repairs_unescaped_comma_inside_value(tmp_path):
    path = tmp_path / "ragged.csv"
    path.write_text(
        "id,note,amount\n"
        "1,hello,10\n"
        "2,a,b,20\n"  # note 안에 이스케이프 안 된 쉼표
        "3,world,30\n",
        encoding="utf-8",
    )
    df = read_csv_safely(path)
    assert list(df["note"]) == ["hello", "a,b", "world"]
    # 텍스트로 왕복시켜 dtype 추론을 유지한다 - 전부 object가 되면 프로파일링이 무너진다.
    assert pd.api.types.is_integer_dtype(df["amount"])


def test_unrepairable_row_raises(tmp_path):
    path = tmp_path / "hopeless.csv"
    # 붙어 있는 문자열 컬럼 두 개 - 어느 쪽이 깨졌는지 형식으로 구분할 수 없다.
    path.write_text("a,b,c\nx,y,z\np,q,r\n1,2,3,4,5\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        read_csv_safely(path)
    assert "CSV를 읽지 못했습니다" in str(e.value)


def test_csv_repair_error_is_exported():
    assert issubclass(CsvRepairError, Exception)
