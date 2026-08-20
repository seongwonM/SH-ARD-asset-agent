"""LLM 호출 기록. 어댑터가 남기는 것이라 여기서만 openai 호환 클라이언트를 흉내낸다.

llm_calls 문서의 값어치는 "이 프롬프트에 이 입력을 줬더니 이 응답이 왔다"를
나중에 그대로 재현할 수 있는지에 달려 있다 - 원문이 잘리거나 빠지면 의미가 없다.
"""

from __future__ import annotations

import pytest

from column_semantics.adapters.llm import OpenAICompatibleLLM
from column_semantics.core.llm_log import LLMLog


class StubClient:
    """openai 클라이언트의 chat.completions.create만 흉내낸다."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = type("chat", (), {"completions": self})()

    def create(self, model, messages):
        self.requests.append({"model": model, "messages": messages})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        message = type("m", (), {"content": reply})()
        choice = type("c", (), {"message": message})()
        return type("r", (), {"choices": [choice], "usage": type("u", (), {"total_tokens": 42})()})()


def make(replies):
    log = LLMLog()
    llm = OpenAICompatibleLLM(client=StubClient(replies), model="stub-model", llm_log=log)
    return llm, log


def test_records_prompt_payload_and_raw_response():
    llm, log = make(['{"ok": true}'])
    llm.complete_json(
        "# semantic_type 프롬프트",
        {"table": {"columns": ["a"]}},
        label="semantic_type",
        context={"name": "semantic_type", "phase": "exec", "round": 1},
    )

    call = log.calls()[0]
    assert call["seq"] == 1
    assert call["status"] == "ok"
    assert call["name"] == "semantic_type"
    assert call["phase"] == "exec"
    assert call["input"] == {"table": {"columns": ["a"]}}
    assert call["output_text"] == '{"ok": true}'
    assert call["output"] == {"ok": True}
    assert call["tokens"] == 42
    # system 프롬프트는 호출마다 복사하지 않고 한 벌만 두고 참조한다.
    assert log.prompts()[call["prompt_ref"]] == "# semantic_type 프롬프트"


def test_same_prompt_is_stored_once_across_columns():
    llm, log = make(['{"a": 1}', '{"b": 2}'])
    for column in ("a", "b"):
        llm.complete_json(
            "# column_interpretation 프롬프트",
            {"target_column": column},
            label=f"column_interpretation:{column}",
            context={"name": "column_interpretation", "column": column},
        )

    assert len(log.calls()) == 2
    assert list(log.prompts()) == ["column_interpretation"]
    assert [c["column"] for c in log.calls()] == ["a", "b"]


def test_failed_attempt_is_recorded_with_whatever_came_back():
    """JSON이 아니어서 재시도한 경우, 실패한 응답 원문도 남아야 원인을 볼 수 있다."""
    llm, log = make(["설명하자면...", '{"ok": true}'])
    llm.complete_json("# p", {"x": 1}, label="planner", max_retries=1)

    first, second = log.calls()
    assert first["status"] == "error"
    assert first["output_text"] == "설명하자면..."
    assert first["output"] is None
    assert "JSON 응답 파싱 실패" in first["error"]
    assert second["status"] == "ok" and second["attempt"] == 2


def test_giving_up_raises_after_recording_every_attempt():
    llm, log = make([RuntimeError("연결 실패"), RuntimeError("또 실패")])
    with pytest.raises(RuntimeError):
        llm.complete_json("# p", {"x": 1}, label="planner", max_retries=1)

    assert [c["status"] for c in log.calls()] == ["error", "error"]
    # 응답을 아예 못 받은 실패는 output_text가 None이다(빈 문자열과 구분된다).
    assert log.calls()[0]["output_text"] is None
