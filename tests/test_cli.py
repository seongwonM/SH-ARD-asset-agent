"""CLI 경로 - k8s Job이 실제로 실행하는 그 경로다.

    python run.py <csv> --skills <dir> --output <out>

여기가 깨지면 배치가 통째로 죽으므로, 인자 계약과 산출물 파일까지 확인한다.
LLM만 가짜로 바꾸고 나머지(CSV 읽기, skills 폴더 로딩, 결과 쓰기)는 진짜다.
"""

from __future__ import annotations

import json

from conftest import SKILL_DIR
from fakes import FakeLLM

from column_semantics import app, cli


def test_cli_writes_result_and_cleans_up_checkpoint(tmp_path, equipment_csv, monkeypatch):
    monkeypatch.setattr(app, "make_llm_from_env", lambda **kwargs: FakeLLM())
    out = tmp_path / "result.semantic.json"

    cli.main([str(equipment_csv), "--skills", str(SKILL_DIR), "--output", str(out), "--max-rounds", "1"])

    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["meta"]["status"] == "done"
    assert result["meta"]["source_csv"] == str(equipment_csv)
    assert result["results"]["table_context"]["grain"]
    # 성공하면 체크포인트는 남기지 않는다.
    assert not (tmp_path / "result.semantic.json.partial.json").exists()


def test_checkpoint_survives_a_mid_run_failure(tmp_path, equipment_csv, monkeypatch):
    class Exploding(FakeLLM):
        def _on_gap_planner(self, label, payload):
            raise RuntimeError("엔드포인트 죽음")

    monkeypatch.setattr(app, "make_llm_from_env", lambda **kwargs: Exploding())
    out = tmp_path / "result.semantic.json"
    checkpoint = tmp_path / "result.semantic.json.partial.json"

    try:
        cli.main([str(equipment_csv), "--skills", str(SKILL_DIR), "--output", str(out)])
    except RuntimeError:
        pass

    assert not out.exists()
    partial = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert partial["meta"]["status"] == "in_progress"
    # 죽기 전까지 계산된 skill 출력은 남아 있어야 한다.
    assert "semantic_type" in partial["results"]
    assert partial["results"]["column_interpretation"]["columns"]


def test_default_skill_dir_points_at_repo_skills():
    assert app.DEFAULT_SKILL_DIR == SKILL_DIR
