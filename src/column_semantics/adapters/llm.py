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
from typing import Any, Dict, List, Optional, Protocol

from column_semantics.core.clock import now_iso
from column_semantics.core.jsonx import clean_for_json
from column_semantics.core.llm_log import LLMLog
from column_semantics.adapters.ratelimit import RateLimiter


# 호출 하나가 얼마나 버티는가. 전부 환경변수로 조정하고(LLM_ 접두사라 meta의
# env 스냅샷에 자동으로 실린다), 기본값은 지금까지 돌던 것과 같다.
#
#   LLM_MAX_RETRIES           이 어댑터의 재시도 횟수. JSON 파싱 실패까지 포함해
#                             "무슨 실패든" 다시 던진다. 총 시도 = 이 값 + 1.
#   LLM_RETRY_BACKOFF_SECONDS 재시도 사이 대기(지수: 5 -> 10 -> 20 ...). 0이면 즉시.
#   LLM_TIMEOUT_SECONDS       호출 하나의 상한. 큰 테이블에서 응답이 느리면 늘린다.
#   LLM_HTTP_RETRIES          openai SDK가 연결 오류/429/5xx에 대해 자체적으로 무는
#                             재시도. 위 재시도의 **안쪽** 층이라 총 시도는 곱해진다.
#
# 층이 둘인 것은 잡는 실패가 다르기 때문이다 - SDK는 응답을 못 받은 경우만 알고,
# 응답을 받았는데 JSON이 아니었던 경우는 여기서만 다시 던질 수 있다.
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF = 5.0
DEFAULT_TIMEOUT = 600.0
DEFAULT_HTTP_RETRIES = 2


def _env_number(name: str, default: float) -> float:
    """빈 문자열/오타를 기본값으로 되돌린다 - 주말 배치가 설정 오타 하나로
    시작하자마자 죽는 것보다는, 무엇으로 돌았는지 로그에 남기고 도는 편이 낫다."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[LLM] {name}={raw!r} 를 숫자로 읽지 못해 기본값 {default}을 씁니다.", flush=True)
        return default


class LLMClient(Protocol):
    model: str

    def complete_json(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        *,
        label: str = "",
        max_retries: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """JSON object 하나를 받아 dict로 돌려준다. 끝내 실패하면 예외.

        `max_retries`가 None이면 어댑터가 환경변수로 잡아둔 값을 쓴다 - 재시도
        정책은 호출부가 아니라 어댑터의 설정이다.

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
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        call_settings: Optional[Dict[str, Any]] = None,
    ):
        self.client = client
        self.model = model
        self.llm_log = llm_log
        self.rate_limiter = rate_limiter
        # 재시도 정책은 어댑터가 갖는다 - 호출부(파이프라인)가 단계마다 다른
        # 횟수를 정하기 시작하면 "이 실행이 몇 번까지 버텼는가"를 한 곳에서
        # 말할 수 없게 된다.
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        # meta/로그에 그대로 실리는 실행 설정. 재시도·타임아웃이 결과(끝까지
        # 돌았는지)를 바꾸므로 남겨야 한다.
        self.call_settings: Dict[str, Any] = call_settings or {
            "max_retries": self.max_retries,
            "retry_backoff_seconds": self.retry_backoff,
        }

    def complete_json(
        self,
        system_prompt: str,
        payload: Dict[str, Any],
        *,
        label: str = "",
        max_retries: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        max_retries = self.max_retries if max_retries is None else max_retries
        clean_payload = clean_for_json(payload)
        user_text = json.dumps(clean_payload, ensure_ascii=False, indent=2)
        tag = f"[LLM:{label}]" if label else "[LLM]"
        context = {"label": label, **(context or {})}
        prompt_ref = (
            self.llm_log.register_prompt(str(context.get("name") or label or "unnamed"), system_prompt)
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

            # 대기는 슬롯을 놓은 **뒤에** 한다. 재시도를 기다리는 동안 동시성
            # 한 자리를 붙들고 있으면 멀쩡한 다른 컬럼 호출이 그만큼 막힌다.
            # 엔드포인트가 잠깐 죽은 경우가 재시도의 주 용도라, 간격 없이 바로
            # 다시 던지는 것은 시도 횟수만 태우는 일이다(지수 백오프).
            if attempt < max_retries and self.retry_backoff > 0:
                wait = self.retry_backoff * (2**attempt)
                print(f"{tag} {wait:.0f}초 후 재시도", flush=True)
                time.sleep(wait)

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


def models_from_env() -> List[str]:
    """LLM_MODEL을 모델 목록으로 읽는다. 쉼표로 여러 개를 적을 수 있다.

        LLM_MODEL=modelA              -> ["modelA"]
        LLM_MODEL=modelA, modelB      -> ["modelA", "modelB"]

    여러 모델을 도는 것은 배치/실험의 일이고, 실행 하나는 항상 모델 하나다 -
    한 결과 폴더 안에 두 모델의 출력이 섞이면 어느 쪽이 낸 건지 알 수 없다.
    """
    raw = os.getenv("LLM_MODEL", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def make_llm_from_env(
    llm_log: Optional[LLMLog] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> OpenAICompatibleLLM:
    """LLM_API_ENDPOINT / LLM_API_KEY / LLM_MODEL로 클라이언트를 만든다.

    k8s에서는 secret `sh-ard-asset-agent-secret`이 envFrom으로 주입하고,
    로컬에서는 .env를 `load_dotenv`가 읽는다.

    LLM_MODEL에 여러 개가 적혀 있으면 첫 번째만 쓴다(한 실행 = 한 모델). 조용히
    고르면 결과를 나중에 볼 때 어느 모델이었는지 헷갈리므로 로그로 알린다.
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai 패키지를 사용할 수 없습니다. `pip install -U openai`로 설치하세요."
        ) from e

    endpoint = os.getenv("LLM_API_ENDPOINT")
    api_key = os.getenv("LLM_API_KEY", "EMPTY")
    models = models_from_env()
    model = models[0] if models else None
    if len(models) > 1:
        print(
            f"[LLM] LLM_MODEL에 {len(models)}개가 지정됨 - 이 실행은 첫 번째({model})만 쓴다. "
            "모델별로 돌리려면 배치(experiments/run_batch.py, k8s Job)를 쓸 것.",
            flush=True,
        )

    missing = [
        name
        for name, value in [("LLM_API_ENDPOINT", endpoint), ("LLM_MODEL", model)]
        if not value
    ]
    if missing:
        raise RuntimeError("필수 환경변수가 없습니다: " + ", ".join(missing))

    timeout = _env_number("LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)
    http_retries = int(_env_number("LLM_HTTP_RETRIES", DEFAULT_HTTP_RETRIES))
    max_retries = int(_env_number("LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES))
    retry_backoff = _env_number("LLM_RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF)
    call_settings = {
        "max_retries": max(0, max_retries),
        "retry_backoff_seconds": max(0.0, retry_backoff),
        "timeout_seconds": timeout,
        "http_retries": http_retries,
    }

    return OpenAICompatibleLLM(
        client=OpenAI(
            base_url=endpoint,
            api_key=api_key,
            timeout=timeout,
            max_retries=http_retries,
        ),
        model=str(model),
        llm_log=llm_log,
        rate_limiter=rate_limiter,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        call_settings=call_settings,
    )


def make_rate_limiter_from_env() -> RateLimiter:
    return RateLimiter(
        requests_per_minute=int(os.environ.get("LLM_REQUESTS_PER_MINUTE", "360")),
        max_concurrency=int(os.environ.get("LLM_MAX_CONCURRENCY", "12")),
    )
