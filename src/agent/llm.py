"""
LLM 접근 및 외부 자원.

vLLM structured output API 표기는 버전에 따라 다르다.
  구형: extra_body={"guided_json": schema, "guided_decoding_backend": "xgrammar"}
  신형: response_format={"type": "json_schema", "json_schema": {...}}
STRUCTURED_MODE로 전환한다. 사내 서빙 버전 문서로 확정할 것.

guided decoding 백엔드가 처리하지 못하는 스키마 요소가 있으므로,
Pydantic 모델은 다음 규칙으로 작성한다(skills/_template/SKILL.md에도 명시).
  - Optional/Union 금지 → "값 없음"은 빈 문자열
  - Literal로 라벨 고정 → 시맨틱 드리프트를 디코딩 단계에서 차단
  - 중첩 2단계 이내, $ref는 inline_refs()로 펼침
  - extra="forbid"
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any, Dict, List, Type

from pydantic import BaseModel, ValidationError

from .contract import SkillDeps

STRUCTURED_MODE = os.getenv("VLLM_STRUCTURED_MODE", "guided_json")
BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
MODEL = os.getenv("VLLM_MODEL", "Qwen/Qwen2.5-32B-Instruct")
MAX_CONCURRENCY = int(os.getenv("VLLM_MAX_CONCURRENCY", "8"))


class LLMError(RuntimeError):
    pass


def inline_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """$defs/$ref를 펼친다. 일부 guided decoding 백엔드가 $ref를 처리하지 못한다."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs.get(node["$ref"].split("/")[-1], {})))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def repair_json(text: str) -> str:
    """잘린 JSON 복구: {...} 추출 → 중괄호 보정 → trailing comma 제거."""
    start = text.find("{")
    if start == -1:
        return text
    body = text[start:]
    end = body.rfind("}")
    if end != -1:
        body = body[: end + 1]
    opened, closed = body.count("{"), body.count("}")
    if opened > closed:
        body += "}" * (opened - closed)
    out, prev = [], ""
    for ch in body:
        if ch == "}" and prev == ",":
            out.pop()
        out.append(ch)
        if not ch.isspace():
            prev = ch
    return "".join(out)


class RuntimeDeps(SkillDeps):
    """운영용 deps. 테스트는 이 클래스를 상속해 structured()만 갈아끼운다."""

    def __init__(self, frames: Dict[str, Any] | None = None):
        self._frames = frames or {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)

    # -- DataFrame -------------------------------------------------------
    def register_frame(self, ref: str, frame) -> str:
        self._frames[ref] = frame
        return ref

    def dataframe(self, ref: str):
        if ref not in self._frames:
            # TODO: db:// , s3:// 등 스킴별 로더 분기
            raise KeyError(f"등록되지 않은 data_ref: {ref}")
        return self._frames[ref]

    # -- LLM -------------------------------------------------------------
    def _payload(self, messages: List[Dict[str, str]], schema: Dict[str, Any], stage: str):
        flat = inline_refs(schema)
        payload: Dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "temperature": 0.0 if stage != "expand" else 0.2,
            "max_tokens": 1024,
        }
        if STRUCTURED_MODE == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": flat.get("title", "out"), "schema": flat, "strict": True},
            }
        else:
            payload["extra_body"] = {"guided_json": flat, "guided_decoding_backend": "xgrammar"}
        return payload

    async def _post(self, payload: Dict[str, Any]) -> str:
        """
        TODO: 실제 구현
            from openai import AsyncOpenAI
            client = AsyncOpenAI(base_url=BASE_URL, api_key="EMPTY")
            extra = payload.pop("extra_body", None)
            r = await client.chat.completions.create(**payload, extra_body=extra)
            return r.choices[0].message.content
        """
        raise NotImplementedError("vLLM 엔드포인트 연결부를 구현하세요")

    async def structured(self, messages: List[Dict[str, str]], model_cls: Type[BaseModel], stage: str):
        payload = self._payload(messages, model_cls.model_json_schema(), stage)
        async with self._sem:
            raw = await self._post(payload)
        try:
            return model_cls.model_validate_json(raw)
        except (ValidationError, ValueError):
            try:
                return model_cls.model_validate(json.loads(repair_json(raw)))
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"structured output 파싱 실패: {exc}") from exc
