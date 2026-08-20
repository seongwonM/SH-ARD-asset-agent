"""LLM 어댑터. OpenAI 호환 엔드포인트(vLLM)를 JSON 호출로 감싼다.

파이프라인은 `LLMClient` 프로토콜 하나만 본다:

    complete_json(system_prompt, payload, label=...) -> dict

그래서 테스트는 openai 패키지도 서버도 없이 돈다. 재시도/레이트리밋/호출
기록은 전부 이 어댑터 안에 있고, 파이프라인 쪽 코드에는 나타나지 않는다.

호출 하나하나의 system 프롬프트·입력 payload·응답 원문은 `LLMLog`에 쌓인다
(결과의 llm_calls 문서가 그 내용 그대로다). 어떤 단계에서 나온 호출인지는
호출부가 `context`로 넘긴다 - 어댑터는 그걸 해석하지 않고 그대로 붙인다.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional, Protocol

from column_semantics.core.clock import now_iso
from column_semantics.core.jsonx import clean_for_json
from column_semantics.core.llm_log import LLMLog
from column_semantics.adapters.ratelimit import RateLimiter


class LLMClient(Protocol):
    model: str

    def complete_json(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        *,
        label: str = "",
        max_retries: int = 1,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """JSON object 하나를 받아 dict로 돌려준다. 끝내 실패하면 예외.

        `context`(skill/column/round/phase 등)는 기록에만 쓰인다 - 호출 결과를
        바꾸지 않는다.
        """


def parse_json_text(text: str) -> Dict[str, Any]:
    text = text.strip()

    # Remove markdown fences if the model ignored "JSON only".
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: extract the outermost object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError(f"JSON 응답 파싱 실패:\n{text[:1500]}")


class OpenAICompatibleLLM:
    def __init__(
        self,
        client: Any,
        model: str,
        llm_log: Optional[LLMLog] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.client = client
        self.model = model
        self.llm_log = llm_log
        self.rate_limiter = rate_limiter

    def complete_json(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        *,
        label: str = "",
        max_retries: int = 1,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        clean_payload = clean_for_json(payload)
        user_text = json.dumps(clean_payload, ensure_ascii=False, indent=2)
        tag = f"[LLM:{label}]" if label else "[LLM]"
        context = {"label": label, **(context or {})}
        prompt_ref = (
            self.llm_log.register_prompt(str(context.get("skill") or label or "unnamed"), system_prompt)
            if self.llm_log is not None
            else ""
        )

        last_error = None
        for attempt in range(max_retries + 1):
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "아래 입력을 처리하고 반드시 JSON object 하나만 반환하세요.\n\n" + user_text
                    ),
                },
            ]
            if attempt > 0:
                messages.append(
                    {
                        "role": "user",
                        "content": "이전 응답은 JSON 파싱에 실패했습니다. 설명/마크다운 없이 유효한 JSON만 반환하세요.",
                    }
                )

            started_at = now_iso()
            print(
                f"{tag} 요청 전송 (시도 {attempt + 1}/{max_retries + 1}, "
                f"model={self.model}, 입력 {len(user_text):,}자)",
                flush=True,
            )
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            started = time.time()
            text = None
            # 응답을 못 받은 실패(연결/타임아웃)와 받았지만 JSON이 아니었던 실패를
            # 같은 자리에서 잡는다 - 기록에는 그 차이가 output_text로 남는다.
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                )
                text = resp.choices[0].message.content or ""
                elapsed = time.time() - started
                usage = getattr(resp, "usage", None)
                total_tokens = usage.total_tokens if usage else None
                tokens = f", 토큰 {total_tokens}" if total_tokens is not None else ""
                print(f"{tag} 응답 수신 ({elapsed:.1f}초, 출력 {len(text):,}자{tokens})", flush=True)
                parsed = parse_json_text(text)
                self._record(
                    prompt_ref=prompt_ref,
                    payload=clean_payload,
                    response_text=text,
                    response=parsed,
                    context=context,
                    attempt=attempt + 1,
                    started_at=started_at,
                    elapsed=elapsed,
                    status="ok",
                    input_chars=len(user_text),
                    output_chars=len(text),
                    tokens=total_tokens,
                )
                return parsed
            except Exception as e:  # noqa: BLE001 - 어떤 실패든 재시도 후 상위로 올린다
                elapsed = time.time() - started
                last_error = e
                print(f"{tag} 실패 ({elapsed:.1f}초): {type(e).__name__}: {e}", flush=True)
                self._record(
                    prompt_ref=prompt_ref,
                    payload=clean_payload,
                    response_text=text,
                    response=None,
                    context=context,
                    attempt=attempt + 1,
                    started_at=started_at,
                    elapsed=elapsed,
                    status="error",
                    input_chars=len(user_text),
                    output_chars=None if text is None else len(text),
                    error=f"{type(e).__name__}: {e}",
                )
            finally:
                if self.rate_limiter is not None:
                    self.rate_limiter.release()

        raise RuntimeError(f"LLM 호출 실패: {last_error}")

    def _record(self, *, started_at: str, elapsed: float, **fields: Any) -> None:
        if self.llm_log is None:
            return
        self.llm_log.add(
            model=self.model,
            started_at=started_at,
            finished_at=now_iso(),
            elapsed_seconds=round(elapsed, 3),
            **fields,
        )


def make_llm_from_env(
    llm_log: Optional[LLMLog] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> OpenAICompatibleLLM:
    """LLM_API_ENDPOINT / LLM_API_KEY / LLM_MODEL로 클라이언트를 만든다.

    k8s에서는 secret `sh-ard-asset-agent-secret`이 envFrom으로 주입하고,
    로컬에서는 .env를 `load_dotenv`가 읽는다.
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai 패키지를 사용할 수 없습니다. `pip install -U openai`로 설치하세요."
        ) from e

    endpoint = os.getenv("LLM_API_ENDPOINT")
    api_key = os.getenv("LLM_API_KEY", "EMPTY")
    model = os.getenv("LLM_MODEL")

    missing = [
        name
        for name, value in [("LLM_API_ENDPOINT", endpoint), ("LLM_MODEL", model)]
        if not value
    ]
    if missing:
        raise RuntimeError("필수 환경변수가 없습니다: " + ", ".join(missing))

    return OpenAICompatibleLLM(
        client=OpenAI(base_url=endpoint, api_key=api_key),
        model=str(model),
        llm_log=llm_log,
        rate_limiter=rate_limiter,
    )


def make_rate_limiter_from_env() -> RateLimiter:
    return RateLimiter(
        requests_per_minute=int(os.environ.get("LLM_REQUESTS_PER_MINUTE", "360")),
        max_concurrency=int(os.environ.get("LLM_MAX_CONCURRENCY", "12")),
    )
