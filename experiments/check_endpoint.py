#!/usr/bin/env python3
"""LLM 엔드포인트 점검. 파이프라인을 돌리기 전에 여기서 막힌 곳을 먼저 본다.

확인 항목
  1. 환경변수(LLM_API_ENDPOINT / LLM_MODEL)가 채워져 있는지
  2. /v1/models에 지정한 모델이 실제로 서빙되고 있는지
  3. skill과 같은 방식(system 프롬프트 + "JSON object 하나만")으로 물었을 때
     파싱 가능한 JSON이 오는지

3번이 중요하다. 이 파이프라인은 structured output 옵션을 쓰지 않고 프롬프트로만
JSON을 요구한 뒤 파싱 실패 시 한 번 재시도한다. 즉 "JSON을 안 내는 모델"은
에러가 아니라 느린 실패로 나타난다 - 미리 확인해두는 편이 싸다.

    python experiments/check_endpoint.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from column_semantics.adapters.env import load_dotenv  # noqa: E402
from column_semantics.adapters.llm import make_llm_from_env  # noqa: E402


def main() -> int:
    load_dotenv()

    endpoint = os.getenv("LLM_API_ENDPOINT")
    model = os.getenv("LLM_MODEL")
    print(f"endpoint = {endpoint}")
    print(f"model    = {model}")
    if not endpoint or not model:
        print("[FAIL] LLM_API_ENDPOINT / LLM_MODEL이 필요합니다 (.env 또는 환경변수).")
        return 1

    llm = make_llm_from_env()

    try:
        served = [m.id for m in llm.client.models.list().data]
        print(f"[OK] 서빙 중인 모델: {served}")
        if model not in served:
            print(f"[WARN] LLM_MODEL({model})이 목록에 없습니다. 이름이 다르면 호출이 실패합니다.")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] /v1/models 조회 실패: {type(e).__name__}: {e} (엔드포인트가 목록을 안 열 수도 있음)")

    try:
        out = llm.complete_json(
            "너는 JSON만 반환한다. 다른 텍스트를 붙이지 않는다.",
            {"task": "아래 값을 그대로 echo 필드에 넣어 반환하라", "value": "ping"},
            label="check",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] JSON 호출 실패: {type(e).__name__}: {e}")
        return 1

    print(f"[OK] JSON 파싱 성공: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
