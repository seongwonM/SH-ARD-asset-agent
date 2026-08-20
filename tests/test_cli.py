"""CLI 경로 - k8s Job이 실제로 실행하는 그 경로다.

    python run.py <csv> --skills <dir> --output <out>

여기가 깨지면 배치가 통째로 죽으므로, 인자 계약과 산출물 파일까지 확인한다.
LLM만 가짜로 바꾸고 나머지(CSV 읽기, skills 폴더 로딩, 결과 쓰기)는 진짜다.
"""

from __future__ import annotations

import json

from conftest import PROMPT_DIR, SKILL_DIR
from fakes import FakeLLM

from column_semantics import app, cli
from column_semantics.pipeline.documents import PARTS


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cli_writes_one_file_per_document(tmp_path, equipment_csv, monkeypatch):
    monkeypatch.setattr(app, "make_llm_from_env", lambda **kwargs: FakeLLM(llm_log=kwargs["llm_log"]))
    out = tmp_path / "result.semantic.json"

    cli.main(
        [
            str(equipment_csv),
            "--prompts", str(PROMPT_DIR),
            "--skills", str(SKILL_DIR),
            "--output", str(out),
            "--max-rounds", "1",
        ]
    )

    paths = app.output_paths(out)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [equipment_csv.name] + [paths[part].name for part in PARTS]
    )

    columns = read(paths["columns"])
    assert columns["meta"]["status"] == "done"
    assert columns["meta"]["source_csv"] == str(equipment_csv)
    # 처리량 설정과 입력 크기도 실행 설정이다 - 결과 비교에 필요하다.
    assert columns["meta"]["row_count"] == 12
    assert columns["meta"]["column_count"] == 6
    assert columns["meta"]["requests_per_minute"] >= 1
    assert columns["meta"]["max_concurrency"] >= 1
    assert "llm_endpoint" in columns["meta"]
    assert columns["columns"]["power_value"]["stages"]
    assert read(paths["table"])["table_context"]["grain"]
    assert read(paths["rulebase"])["column_profiles"]["run_id"]["name"] == "run_id"
    assert read(paths["plan"])["first_pass"]["stages"][0] == "semantic_type"

    calls = read(paths["llm_calls"])
    assert calls["prompts"]["column_interpretation"].startswith("#")
    interpretations = [c for c in calls["calls"] if c["name"] == "column_interpretation"]
    assert len(interpretations) == 6  # 컬럼 수만큼
    assert interpretations[0]["input"]["target_column"] in columns["columns"]
    assert json.loads(interpretations[0]["output_text"]) == interpretations[0]["output"]


def test_files_survive_a_mid_run_failure(tmp_path, equipment_csv, monkeypatch):
    """중간에 죽어도 그때까지의 문서가 파일에 남고, 죽은 이유와 죽인 호출까지 남는다."""

    class Exploding(FakeLLM):
        def _on_gap_planner(self, label, payload):
            raise RuntimeError("엔드포인트 죽음")

    monkeypatch.setattr(
        app, "make_llm_from_env", lambda **kwargs: Exploding(llm_log=kwargs["llm_log"])
    )
    out = tmp_path / "result.semantic.json"

    try:
        cli.main(
            [
                str(equipment_csv),
                "--prompts", str(PROMPT_DIR),
                "--skills", str(SKILL_DIR),
                "--output", str(out),
            ]
        )
    except RuntimeError:
        pass

    paths = app.output_paths(out)
    columns = read(paths["columns"])
    assert columns["meta"]["status"] == "failed"
    assert "엔드포인트 죽음" in columns["meta"]["error"]
    # 죽기 전까지 계산된 해석은 남아 있어야 한다.
    assert columns["columns"]["power_value"]["final"]["interpretation"]
    assert read(paths["rulebase"])["column_profiles"]

    # 죽인 그 호출도 파일에 남아야 한다 - 마지막 체크포인트 이후의 기록이
    # 통째로 사라지면 무엇 때문에 멈췄는지 파일만 보고는 알 수 없다.
    calls = read(paths["llm_calls"])["calls"]
    crashed = [c for c in calls if c["status"] == "error"]
    assert [c["name"] for c in crashed] == ["gap_planner"]
    assert "엔드포인트 죽음" in crashed[0]["error"]


def test_default_dirs_point_at_the_repo_folders():
    assert app.DEFAULT_PROMPT_DIR == PROMPT_DIR
    assert app.DEFAULT_SKILL_DIR == SKILL_DIR
