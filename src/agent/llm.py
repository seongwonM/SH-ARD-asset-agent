"""
LLM 접근 및 외부 자원.

structured output 모드 3가지
---------------------------
어느 엔드포인트를 쓰느냐에 따라 스키마 강제 방식이 다르다.

| 모드 | 대상 | 강제 수준 |
|---|---|---|
| `guided_json` | vLLM (구형) | 디코딩 단계에서 문법 강제 |
| `json_schema` | vLLM (신형), OpenAI | API가 스키마 검증 |
| `prompt` | 아무 OpenAI 호환 엔드포인트 | 강제 없음. 프롬프트로 요청 + 파싱 재시도 |

`prompt` 모드가 있어야 사내 vLLM 없이도 외부에서 돌려볼 수 있다.
강제가 없으니 파싱 실패율이 올라가지만, 실패는 재시도로 흡수되고
그 횟수 자체가 측정 대상이 된다(bench의 `parse_retry_rate`).

**모드가 틀려도 에러가 나지 않는다.** 옵션이 조용히 무시되고 강제 없이 돈다.
`bench/run_bench.py --probe-mode`로 실제 강제 여부를 확인할 수 있다.

RPM 스로틀
---------
사내 Gateway든 외부 API든 분당 호출 한도가 있다. 배치 전체가 하나의
스로틀 상태를 공유해야 한도가 지켜진다 - 조합마다 새 클라이언트를 만들면
마지막 호출 시각이 매번 리셋되어 한도를 넘긴다.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Type

from pydantic import BaseModel, ValidationError

from .contract import SkillDeps

logger = logging.getLogger(__name__)

# --- 환경변수 --------------------------------------------------------------
STRUCTURED_MODE = os.getenv("LLM_STRUCTURED_MODE", "prompt")
BASE_URL = os.getenv("LLM_API_ENDPOINT", os.getenv("OPENAI_BASE_URL", ""))
API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "EMPTY"))
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "8"))
REQUESTS_PER_MINUTE = int(os.getenv("LLM_REQUESTS_PER_MINUTE", "360"))
MAX_HTTP_RETRIES = int(os.getenv("LLM_MAX_HTTP_RETRIES", "3"))


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 스키마 유틸
# ---------------------------------------------------------------------------


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


def strict_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    OpenAI json_schema strict 모드 요구사항 적용.
    모든 object에 additionalProperties:false, required에 전체 속성.
    """
    schema = inline_refs(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def repair_json(text: str) -> str:
    """잘린 JSON 복구: 코드펜스 제거 → {...} 추출 → 중괄호 보정 → trailing comma 제거."""
    if not text:
        return text
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if len(text.split("```")) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
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


def schema_instruction(model_cls: Type[BaseModel]) -> str:
    """prompt 모드에서 system 프롬프트에 붙일 스키마 설명."""
    schema = inline_refs(model_cls.model_json_schema())
    return (
        "다음 JSON Schema를 정확히 만족하는 JSON object 하나만 출력한다. "
        "스키마에 없는 key를 추가하지 않는다. 코드 블록으로 감싸지 않는다.\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# 스로틀
# ---------------------------------------------------------------------------


class _Throttle:
    """분당 호출 수 상한. 호출을 '보내는 시점' 간격만 강제하고 응답 대기 중엔 락을 푼다."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self.interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._last = now


# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------


class RuntimeDeps(SkillDeps):
    """
    운영용 deps. 테스트는 이 클래스를 상속해 structured()만 갈아끼운다.

    raw_client를 받는 이유: 배치 실행에서 모든 조합이 하나의 클라이언트와
    스로틀 상태를 공유해야 Gateway 한도가 전체 기준으로 지켜진다.
    """

    def __init__(
        self,
        frames: Dict[str, Any] | None = None,
        raw_client: Any = None,
        config: Any = None,
        model: str | None = None,
        structured_mode: str | None = None,
        requests_per_minute: int | None = None,
        max_concurrency: int | None = None,
    ):
        self._frames = frames or {}
        self._client = raw_client
        self._config = config
        self._model = model or getattr(config, "semantic_model", None) or MODEL
        self.structured_mode = structured_mode or STRUCTURED_MODE
        self._sem = asyncio.Semaphore(max_concurrency or MAX_CONCURRENCY)
        self._throttle = _Throttle(
            requests_per_minute
            or getattr(config, "requests_per_minute", None)
            or REQUESTS_PER_MINUTE
        )
        self._stats = {
            "llm_call_count": 0,
            "llm_total_latency": 0.0,
            "llm_prompt_tokens": 0,
            "llm_completion_tokens": 0,
            "llm_total_tokens": 0,
            "parse_failures": 0,
            "http_retries": 0,
        }

    # -- 통계 -------------------------------------------------------------
    def get_stats(self) -> Dict[str, Any]:
        """옛 LLMClient.get_stats()와 같은 키 + 신규 항목."""
        n = self._stats["llm_call_count"]
        return {
            "llm_call_count": n,
            "llm_avg_latency_seconds": round(self._stats["llm_total_latency"] / n, 3) if n else 0.0,
            "llm_prompt_tokens": self._stats["llm_prompt_tokens"],
            "llm_completion_tokens": self._stats["llm_completion_tokens"],
            "llm_total_tokens": self._stats["llm_total_tokens"],
            "parse_failures": self._stats["parse_failures"],
            "http_retries": self._stats["http_retries"],
        }

    def reset_stats(self) -> None:
        for k in self._stats:
            self._stats[k] = 0 if isinstance(self._stats[k], int) else 0.0

    # -- DataFrame --------------------------------------------------------
    def register_frame(self, ref: str, frame) -> str:
        self._frames[ref] = frame
        return ref

    def dataframe(self, ref: str):
        if ref not in self._frames:
            # TODO: db:// , s3:// 등 스킴별 로더 분기
            raise KeyError(f"등록되지 않은 data_ref: {ref}")
        return self._frames[ref]

    # -- 요청 조립 ---------------------------------------------------------
    def _payload(self, messages: List[Dict[str, str]], model_cls: Type[BaseModel], stage: str):
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "temperature": 0.2 if stage == "expand" else 0.0,
            "max_tokens": 1400 if stage == "expand" else 900,
        }
        mode = self.structured_mode

        if mode == "json_schema":
            flat = strict_schema(model_cls.model_json_schema())
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": model_cls.__name__, "schema": flat, "strict": True},
            }
        elif mode == "guided_json":
            payload["extra_body"] = {
                "guided_json": inline_refs(model_cls.model_json_schema()),
                "guided_decoding_backend": os.getenv("LLM_GUIDED_BACKEND", "xgrammar"),
            }
        else:  # prompt
            # API 강제가 없으므로 스키마를 프롬프트에 싣는다.
            msgs = payload["messages"]
            msgs[0] = {
                "role": msgs[0]["role"],
                "content": msgs[0]["content"] + "\n\n" + schema_instruction(model_cls),
            }
            # json_object라도 지원하면 최소한 JSON은 보장된다(키 구조는 아님).
            if os.getenv("LLM_USE_JSON_OBJECT", "1") == "1":
                payload["response_format"] = {"type": "json_object"}
        return payload

    # -- 호출 -------------------------------------------------------------
    async def _post(self, payload: Dict[str, Any]) -> str:
        if self._client is None:
            raise LLMError(
                "LLM 클라이언트가 없습니다. RuntimeDeps(raw_client=OpenAI(...))로 주입하거나 "
                "examples/run_local.py의 build_deps()를 쓰세요."
            )

        create = self._client.chat.completions.create
        extra = payload.pop("extra_body", None)
        kwargs = dict(payload)
        if extra is not None:
            kwargs["extra_body"] = extra

        last_exc: Exception | None = None
        for attempt in range(MAX_HTTP_RETRIES):
            await self._throttle.acquire()
            started = time.time()
            logger.info("llm_call_dispatch model=%s attempt=%d", self._model, attempt + 1)
            try:
                if asyncio.iscoroutinefunction(create):
                    resp = await create(**kwargs)
                else:
                    resp = await asyncio.to_thread(lambda: create(**kwargs))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._stats["http_retries"] += 1
                logger.warning(
                    "llm_call_retry model=%s attempt=%d/%d error=%s: %s",
                    self._model, attempt + 1, MAX_HTTP_RETRIES, type(exc).__name__, exc,
                )
                if attempt == MAX_HTTP_RETRIES - 1:
                    break
                # 429/5xx는 지수 백오프. 지터를 넣어 배치가 동시에 재시도하지 않게 한다.
                await asyncio.sleep((2**attempt) * 0.5 + random.random() * 0.3)
                continue

            latency = time.time() - started
            self._stats["llm_call_count"] += 1
            self._stats["llm_total_latency"] += latency
            usage = getattr(resp, "usage", None)
            prompt_tokens = completion_tokens = total_tokens = 0
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                total_tokens = getattr(usage, "total_tokens", 0) or 0
                self._stats["llm_prompt_tokens"] += prompt_tokens
                self._stats["llm_completion_tokens"] += completion_tokens
                self._stats["llm_total_tokens"] += total_tokens
            logger.info(
                "llm_call model=%s attempt=%d latency_ms=%.0f prompt_tokens=%d "
                "completion_tokens=%d total_tokens=%d",
                self._model, attempt + 1, latency * 1000,
                prompt_tokens, completion_tokens, total_tokens,
            )
            return resp.choices[0].message.content or ""

        logger.error(
            "llm_call_failed model=%s retries=%d error=%s: %s",
            self._model, MAX_HTTP_RETRIES, type(last_exc).__name__, last_exc,
        )
        raise LLMError(f"LLM 호출 실패({MAX_HTTP_RETRIES}회 재시도): {type(last_exc).__name__}: {last_exc}")

    async def structured(self, messages, model_cls: Type[BaseModel], stage: str):
        payload = self._payload(messages, model_cls, stage)
        logger.info("llm_call_queued stage=%s model=%s", stage, self._model)
        async with self._sem:
            raw = await self._post(payload)
        try:
            return model_cls.model_validate_json(raw)
        except (ValidationError, ValueError):
            try:
                parsed = model_cls.model_validate(json.loads(repair_json(raw)))
                self._stats["parse_failures"] += 1  # 복구는 됐지만 1차 파싱은 실패
                return parsed
            except Exception as exc:  # noqa: BLE001
                self._stats["parse_failures"] += 1
                raise LLMError(f"structured output 파싱 실패: {exc}\nraw={raw[:400]}") from exc
