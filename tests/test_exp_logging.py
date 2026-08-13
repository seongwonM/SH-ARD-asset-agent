"""exp 폴더 자동 증가 로직 테스트."""

from __future__ import annotations

from pathlib import Path

from agent.exp_logging import new_exp_dir


def test_new_exp_dir_starts_at_1(tmp_path: Path):
    exp_dir = new_exp_dir(tmp_path)
    assert exp_dir.name.startswith("exp1_")
    assert exp_dir.is_dir()


def test_new_exp_dir_increments(tmp_path: Path):
    first = new_exp_dir(tmp_path)
    second = new_exp_dir(tmp_path)
    assert first.name.startswith("exp1_")
    assert second.name.startswith("exp2_")


def test_new_exp_dir_ignores_unrelated_dirs(tmp_path: Path):
    (tmp_path / "not_an_exp_dir").mkdir()
    (tmp_path / "experiment_notes").mkdir()
    exp_dir = new_exp_dir(tmp_path)
    assert exp_dir.name.startswith("exp1_")


def test_new_exp_dir_resumes_max_number(tmp_path: Path):
    (tmp_path / "exp3_202601010000").mkdir()
    (tmp_path / "exp7_202601020000").mkdir()
    exp_dir = new_exp_dir(tmp_path)
    assert exp_dir.name.startswith("exp8_")
