"""
vLLM 엔드포인트 점검.

structured output 옵션은 **틀려도 에러가 나지 않는다.** 지원하지 않는 표기를
보내면 서버가 조용히 무시하고 평범한 생성으로 돌아간다. 그러면 스키마 강제가
전혀 걸리지 않은 채 파이프라인이 "정상" 동작하는 것처럼 보인다.

그래서 추측하지 말고 직접 찔러본다.

    python bench/check_endpoint.py
    python bench/check_endpoint.py --model Qwen/Qwen2.5-32B-Instruct

`.env` 파일이 있으면 자동으로 읽는다(다른 스크립트와 동일한 파서, LLM_API_ENDPOINT
/ LLM_API_KEY / LLM_MODEL을 export 없이 바로 씀). --endpoint/--api-key/--model
CLI 인자가 있으면 그게 우선한다.

검사 항목
  1. /v1/models 로 서빙 중인 모델 확인
  2. 세 가지 모드(guided_json / json_schema / prompt)를 각각 시도
  3. 각 모드에서 **스키마 위반을 유도**하고 실제로 차단되는지 확인

3번이 핵심이다. 단순히 "응답이 왔다"는 강제 여부를 증명하지 않는다.
스키마에 없는 필드를 넣으라고 지시했을 때 넣지 못해야 강제가 걸린 것이다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.config import load_dotenv_file  # noqa: E402
from agent.llm import RuntimeDeps  # noqa: E402

MODES = ["guided_json", "json_schema", "prompt"]


class ProbeSchema(BaseModel):
    """
    강제 여부 판별용 스키마.

    - `verdict`는 Literal이라 다른 값이 나오면 강제가 안 된 것
    - `extra="forbid"` + 프롬프트에서 추가 필드를 요구 → 나오면 강제가 안 된 것
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["yes", "no"]
    reason: str = Field(description="한 문장")


VIOLATION_PROMPT = [
    {
        "role": "system",
        "content": "너는 테스트 대상이다. 사용자의 지시를 최대한 그대로 따른다.",
    },
    {
        "role": "user",
        "content": (
            "다음 JSON을 그대로 출력하라. 형식을 바꾸지 말 것:\n"
            '{"verdict": "maybe", "reason": "테스트", "extra_field": 123, "another": "값"}'
        ),
    },
]


async def check_mode(client, model: str, mode: str) -> dict:
    deps = RuntimeDeps(raw_client=client, model=model, structured_mode=mode, requests_per_minute=0)
    out = {"mode": mode, "reachable": False, "enforced": None, "detail": ""}
    try:
        parsed = await deps.structured(VIOLATION_PROMPT, ProbeSchema, stage="table")
        out["reachable"] = True
        # 파싱에 성공했다는 건 verdict가 yes/no로 왔고 extra 필드가 없다는 뜻.
        # 위반을 지시했는데도 그랬다면 강제가 걸린 것이다.
        out["enforced"] = True
        out["detail"] = f"위반 지시에도 스키마 준수: verdict={parsed.verdict!r}"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "파싱 실패" in msg or "validation" in msg.lower():
            # 모델이 위반한 응답을 냈다 = 강제가 안 걸렸다.
            out["reachable"] = True
            out["enforced"] = False
            out["detail"] = "위반 응답이 그대로 나옴 - 이 모드는 강제되지 않는다"
        else:
            out["detail"] = f"{type(exc).__name__}: {msg[:160]}"
    return out


async def main_async(args) -> int:
    try:
        from openai import OpenAI
    except ImportError:
        print("openai 패키지가 필요합니다: pip install openai")
        return 2

    load_dotenv_file()

    endpoint = args.endpoint or os.environ.get("LLM_API_ENDPOINT")
    api_key = args.api_key or os.environ.get("LLM_API_KEY", "EMPTY")
    if not endpoint:
        print("LLM_API_ENDPOINT가 필요합니다 (예: http://vllm-host:8000/v1)")
        return 2

    client = OpenAI(base_url=endpoint, api_key=api_key)

    print(f"엔드포인트: {endpoint}")
    model = args.model or os.environ.get("LLM_MODEL")
    try:
        served = [m.id for m in client.models.list().data]
        print(f"서빙 모델: {served}")
        if not model:
            model = served[0]
            print(f"  --model 미지정 → {model} 사용")
        elif model not in served:
            print(f"  경고: {model}이 서빙 목록에 없습니다. 그대로 시도합니다.")
    except Exception as exc:  # noqa: BLE001
        print(f"모델 목록 조회 실패({type(exc).__name__}) - 계속 진행합니다.")
        if not model:
            print("LLM_MODEL을 지정하세요.")
            return 2

    print(f"\n{'모드':<14}{'도달':<8}{'강제':<8}설명")
    print("-" * 78)
    results = []
    for mode in MODES:
        r = await check_mode(client, model, mode)
        results.append(r)
        reach = "OK" if r["reachable"] else "실패"
        enf = {True: "예", False: "아니오", None: "-"}[r["enforced"]]
        print(f"{mode:<14}{reach:<8}{enf:<8}{r['detail'][:46]}")

    enforced = [r["mode"] for r in results if r["enforced"]]
    print()
    if enforced:
        pick = enforced[0]
        print(f"권장 설정:  LLM_STRUCTURED_MODE={pick}")
        print(f"            LLM_MODEL={model}")
        print(f"            LLM_API_ENDPOINT={endpoint}")
    else:
        reachable = [r["mode"] for r in results if r["reachable"]]
        if reachable:
            print(f"주의: 어떤 모드도 스키마를 강제하지 못했습니다. {reachable[0]}로 돌릴 수는 있지만,")
            print("      파싱 실패율이 올라가고 재시도 비용이 늘어납니다.")
            print("      vLLM 기동 시 --guided-decoding-backend 옵션을 확인하세요.")
        else:
            print("어떤 모드도 응답을 받지 못했습니다. 엔드포인트/키/모델명을 확인하세요.")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"endpoint": endpoint, "model": model, "results": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0 if enforced else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--json", default=None, help="결과 저장 경로")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
