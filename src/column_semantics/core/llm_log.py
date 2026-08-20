"""LLM 호출의 입력·출력 원문 기록.

`timeline`의 llm_call 이벤트는 글자수와 토큰만 남긴다 - 얼마나 걸렸는지는
알 수 있어도 "무엇을 물어봐서 무엇을 받았는지"는 알 수 없다. 프롬프트를 고치고
결과가 왜 달라졌는지 따지려면 원문이 있어야 해서, 그 원문만 따로 모은다.

system 프롬프트는 skill 하나당 한 벌만 `prompts`에 담고 호출은 이름으로 참조한다.
컬럼 40개짜리 테이블이면 같은 프롬프트가 40번 실려 파일이 통째로 불어난다.

기록만 하고 아무것도 판단하지 않으므로 core에 있다. 실제로 값을 넣는 쪽은
LLM 어댑터 하나뿐이다.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Dict, List, Optional


class LLMLog:
    def __init__(self) -> None:
        self._calls: List[Dict[str, Any]] = []
        self._prompts: Dict[str, str] = {}
        self._lock = threading.Lock()

    def register_prompt(self, name: str, text: str) -> str:
        """프롬프트 본문을 등록하고 참조 키를 돌려준다.

        같은 이름으로 다른 본문이 들어오면(실행 중 skill 파일이 바뀐 경우) 덮어쓰지
        않고 해시를 붙여 둘 다 남긴다 - 어느 본문으로 돈 호출인지가 흐려지면
        기록의 의미가 없다.
        """
        key = name or "unnamed"
        with self._lock:
            existing = self._prompts.get(key)
            if existing is None:
                self._prompts[key] = text
                return key
            if existing == text:
                return key
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
            key = f"{name}@{digest}"
            self._prompts.setdefault(key, text)
            return key

    def add(
        self,
        *,
        prompt_ref: str,
        payload: Dict[str, Any],
        response_text: Optional[str] = None,
        response: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        **fields: Any,
    ) -> None:
        entry: Dict[str, Any] = {**(context or {}), "prompt_ref": prompt_ref, **fields}
        entry["input"] = payload
        entry["output_text"] = response_text
        entry["output"] = response
        with self._lock:
            entry["seq"] = len(self._calls) + 1
            self._calls.append(entry)

    def prompts(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._prompts)

    def calls(self) -> List[Dict[str, Any]]:
        """호출 순서(seq)대로. 병렬 실행이라 완료 순서와는 다를 수 있다."""
        with self._lock:
            return sorted(self._calls, key=lambda c: c["seq"])
