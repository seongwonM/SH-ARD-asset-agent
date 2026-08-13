"""쉼표로 깨진 CSV 복구 단위 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.csv_repair import CsvRepairError, repair_ragged_csv


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_repairs_single_ragged_column(tmp_path: Path):
    csv_path = _write(
        tmp_path,
        "ragged.csv",
        "id,note,amount\n"
        "1,ok,10\n"
        "2,it has, a comma,20\n"
        "3,fine,30\n",
    )
    df = repair_ragged_csv(csv_path)
    assert list(df.columns) == ["id", "note", "amount"]
    assert df.loc[df["id"] == 2, "note"].iloc[0] == "it has, a comma"
    assert df.loc[df["id"] == 2, "amount"].iloc[0] == 20


def test_passes_through_clean_csv(tmp_path: Path):
    csv_path = _write(tmp_path, "clean.csv", "id,note\n1,a\n2,b\n")
    df = repair_ragged_csv(csv_path)
    assert len(df) == 2
    assert list(df["note"]) == ["a", "b"]


def test_raises_when_no_candidate_matches(tmp_path: Path):
    csv_path = _write(
        tmp_path,
        "unrepairable.csv",
        "id,when,amount\n"
        "1,2026-01-01,10\n"
        "2,2026-01-02,20\n"
        "3,not-a-date,not,a,number\n",
    )
    with pytest.raises(CsvRepairError):
        repair_ragged_csv(csv_path)


def test_empty_csv_returns_empty_dataframe(tmp_path: Path):
    csv_path = _write(tmp_path, "empty.csv", "")
    df = repair_ragged_csv(csv_path)
    assert df.empty
