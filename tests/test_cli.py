"""CLI 경로 - k8s Job이 실제로 실행하는 그 경로다.

    python -m column_semantics <csv> --prompts <dir> --skills <dir> --output-root <dir>

여기가 깨지면 배치가 통째로 죽으므로, 인자 계약과 산출물 파일 배치까지 확인한다.
모델별 반복과 결과 폴더 규칙, run.log가 전부 여기 책임이라 셸이 아니라 여기서
검증된다. LLM만 가짜로 바꾸고 나머지(CSV 읽기, 프롬프트 로딩, 결과 쓰기)는 진짜다.
"""

from __future__ import annotations

import json

import pytest
from conftest import PROMPT_DIR, SKILL_DIR
from fakes import FakeLLM

from column_semantics import app, cli
from column_semantics.pipeline.documents import PARTS


@pytest.fixture(autouse=True)
def _single_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "fake-model")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def use_fake(monkeypatch):
    monkeypatch.setattr(
        app, "make_llm_from_env", lambda **kwargs: FakeLLM(llm_log=kwargs["llm_log"])
    )


def test_cli_writes_one_file_per_document(tmp_path, equipment_csv, monkeypatch):
    use_fake(monkeypatch)
    out = tmp_path / "result.semantic.json"

    code = cli.main(
        [
            str(equipment_csv),
            "--prompts", str(PROMPT_DIR),
            "--skills", str(SKILL_DIR),
            "--output", str(out),
            "--max-rounds", "1",
        ]
    )
    assert code == 0

    paths = app.output_paths(out)
    columns = read(paths["columns"])
    assert columns["meta"]["status"] == "done"
    assert columns["meta"]["source_csv"] == str(equipment_csv)
    assert columns["meta"]["row_count"] == 12
    assert columns["meta"]["column_count"] == 6
    assert columns["meta"]["requests_per_minute"] >= 1
    assert columns["columns"]["power_value"]["stages"]
    assert read(paths["table"])["table_context"]["grain"]
    assert read(paths["plan"])["first_pass"]["stages"][0] == "semantic_type"

    calls = read(paths["llm_calls"])
    assert calls["prompts"]["column_interpretation"].startswith("#")
    interpretations = [c for c in calls["calls"] if c["name"] == "column_interpretation"]
    assert len(interpretations) == 6


def test_output_root_splits_by_model_and_keeps_the_log(tmp_path, equipment_csv, monkeypatch):
    """모델이 여럿이면 같은 CSV를 모델마다 돌고, 폴더가 모델로 갈린다."""
    use_fake(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "modelA, vendor/Model-B")
    root = tmp_path / "20260820_120000"

    assert cli.main(
        [
            str(equipment_csv),
            "--prompts", str(PROMPT_DIR),
            "--skills", str(SKILL_DIR),
            "--output-root", str(root),
            "--max-rounds", "1",
        ]
    ) == 0

    expected = {
        "20260820_120000_modelA": "modelA",
        "20260820_120000_vendor-Model-B": "vendor/Model-B",
    }
    assert {p.name for p in tmp_path.iterdir() if p.is_dir()} == set(expected)

    for folder, model in expected.items():
        run_dir = tmp_path / folder / equipment_csv.stem
        names = sorted(p.name for p in run_dir.iterdir())
        assert names == sorted(
            ["run.log"] + [f"result.semantic.{part}.json" for part in PARTS]
        )
        # 로그는 그 실행 옆에 있어야 쓸모가 있다 - 결과만 있고 로그가 없으면
        # 왜 그렇게 나왔는지 되짚을 수 없다.
        log = (run_dir / "run.log").read_text(encoding="utf-8")
        # 그 실행의 설정이 로그 맨 앞에 있어야 한다 - 어느 모델이었는지 포함.
        assert log.startswith("[CONFIG]")
        assert model in log
        assert read(run_dir / "result.semantic.columns.json")["meta"]["status"] == "done"


def test_several_models_without_output_root_is_refused(tmp_path, equipment_csv, monkeypatch):
    """모델이 여럿인데 파일 경로 하나만 주면 서로 덮어쓴다 - 실행 전에 막는다."""
    use_fake(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "modelA,modelB")

    code = cli.main(
        [str(equipment_csv), "--prompts", str(PROMPT_DIR), "--skills", str(SKILL_DIR),
         "--output", str(tmp_path / "result.json")]
    )
    assert code == 2
    assert not list(tmp_path.glob("result*.json"))


def test_one_model_failing_does_not_stop_the_others(tmp_path, equipment_csv, monkeypatch):
    class DiesOnB(FakeLLM):
        def _on_semantic_type(self, label, payload):
            import os

            if os.environ["LLM_MODEL"] == "modelB":
                raise RuntimeError("엔드포인트 죽음")
            return super()._on_semantic_type(label, payload)

    monkeypatch.setattr(
        app, "make_llm_from_env", lambda **kwargs: DiesOnB(llm_log=kwargs["llm_log"])
    )
    monkeypatch.setenv("LLM_MODEL", "modelB,modelA")
    root = tmp_path / "run"

    code = cli.main(
        [str(equipment_csv), "--prompts", str(PROMPT_DIR), "--skills", str(SKILL_DIR),
         "--output-root", str(root), "--max-rounds", "1"]
    )
    assert code == 1  # 실패가 있었음을 종료코드로 알린다

    # 죽은 모델도 그때까지의 문서와 원인이 남고, 멀쩡한 모델은 끝까지 돈다.
    dead = tmp_path / "run_modelB" / equipment_csv.stem
    assert "엔드포인트 죽음" in (dead / "run.log").read_text(encoding="utf-8")
    assert read(dead / "result.semantic.columns.json")["meta"]["status"] == "failed"
    alive = tmp_path / "run_modelA" / equipment_csv.stem
    assert read(alive / "result.semantic.columns.json")["meta"]["status"] == "done"


def test_missing_model_is_refused(tmp_path, equipment_csv, monkeypatch):
    use_fake(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "")
    assert cli.main([str(equipment_csv), "--output", str(tmp_path / "r.json")]) == 2


def test_default_dirs_point_at_the_repo_folders():
    assert app.DEFAULT_PROMPT_DIR == PROMPT_DIR
    assert app.DEFAULT_SKILL_DIR == SKILL_DIR
