"""다중 모델/하이퍼파라미터 env 해석 테스트."""

from __future__ import annotations

from agent.config import get_models, get_reps


def test_get_models_falls_back_to_llm_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL1", raising=False)
    monkeypatch.delenv("LLM_MODEL2", raising=False)
    monkeypatch.delenv("LLM_MODEL3", raising=False)
    monkeypatch.setenv("LLM_MODEL", "solo-model")
    assert get_models() == ["solo-model"]


def test_get_models_uses_numbered_vars_when_present(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "should-not-be-used")
    monkeypatch.setenv("LLM_MODEL1", "model-a")
    monkeypatch.setenv("LLM_MODEL2", "model-b")
    monkeypatch.delenv("LLM_MODEL3", raising=False)
    assert get_models() == ["model-a", "model-b"]


def test_get_models_skips_blank_numbered_vars(monkeypatch):
    monkeypatch.setenv("LLM_MODEL1", "model-a")
    monkeypatch.setenv("LLM_MODEL2", "")
    monkeypatch.delenv("LLM_MODEL3", raising=False)
    assert get_models() == ["model-a"]


def test_get_reps_reads_env(monkeypatch):
    monkeypatch.setenv("ROBUSTNESS_REPS", "20")
    assert get_reps() == 20


def test_get_reps_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("ROBUSTNESS_REPS", raising=False)
    assert get_reps(default=3) == 3
