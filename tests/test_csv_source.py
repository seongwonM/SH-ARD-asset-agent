from __future__ import annotations

import pandas as pd
import pytest

from column_semantics.adapters.csv_repair import repair_rows
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


def test_repairs_thousand_separator_in_numeric_column(tmp_path):
    # 숫자 컬럼의 1,234는 깨진 값이 아니라 천 단위 구분자다. 컬럼의 다른 값들을
    # 그대로 대조하면 "한 번도 못 본 모양"이라 옆 문자열 컬럼이 이겨버린다.
    path = tmp_path / "thousand.csv"
    path.write_text(
        "id,name,amount\n1,alpha,100\n2,beta,200\n3,gamma,1,234\n",
        encoding="utf-8",
    )
    df = read_csv_safely(path)
    assert list(df["name"]) == ["alpha", "beta", "gamma"]
    assert list(df["amount"])[2] == "1,234"


def test_ambiguous_row_is_marked_not_guessed():
    # 어느 컬럼이 깨졌는지 데이터가 정하지 못하면(여기선 세 후보가 동점) 그럴듯한
    # 쪽을 고르지 않는다 - 자동 경로에서 잘못 병합된 값은 그때부터 측정값 행세를 한다.
    header = ["a", "b", "c"]
    rows = [(2, ["x", "y", "z"]), (3, ["p", "q", "r"]), (4, ["1", "2", "3", "4", "5"])]

    records, _ = repair_rows(header, rows)

    assert [r.status for r in records] == ["ok", "ok", "ambiguous"]
    assert records[2].margin == 0.0
    assert records[2].raw == ["1", "2", "3", "4", "5"]


def test_short_row_is_left_alone(tmp_path):
    # 필드가 모자란 행은 쉼표 문제가 아니다(값 누락/줄바꿈). 복구 대상이 아니라
    # 그대로 통과시킨다 - 초과 행 때문에 파일 전체를 거절하지도 않는다.
    path = tmp_path / "short.csv"
    path.write_text("id,note,amount\n1,hello,10\n2,a,b,20\n3,world\n", encoding="utf-8")
    df = read_csv_safely(path)
    assert list(df["note"]) == ["hello", "a,b", "world"]
    assert pd.isna(df["amount"].iloc[2])


def test_csv_repair_error_is_exported():
    assert issubclass(CsvRepairError, Exception)
