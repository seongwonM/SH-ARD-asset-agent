"""프롬프트 본문(markdown) 로더.

프롬프트는 코드가 아니라 파일이다. `<폴더>/<이름>.md` 하나가 프롬프트 하나이고,
파일 내용이 그대로 LLM system 프롬프트가 된다. 등록 절차는 없다 - 폴더를 읽는다.

폴더는 둘이고, 나뉜 기준은 **누가 실행을 결정하는가**다.

    prompts/   고정 단계. 코드가 정한 순서대로 항상(또는 데이터 조건에 따라) 돈다.
    skills/    보완 skill. gap_planner가 그 컬럼에 필요하다고 판단할 때만 붙는다.

로딩 방식이 같다고 한 폴더에 섞어두면 "이건 언제 도는 건가"를 매번 코드에서
확인해야 한다. 폴더가 그 답이다.

파이프라인은 `PromptLibrary` 프로토콜(이름 -> 프롬프트 문자열)에만 의존하므로,
테스트에서는 dict 하나로 대체할 수 있다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Protocol


class PromptLibrary(Protocol):
    def prompt(self, name: str) -> str:
        """프롬프트 본문을 돌려준다. 없으면 KeyError."""


class FileSystemPrompts:
    def __init__(self, directory: Path, required: Iterable[str] = ()):
        self.directory = Path(directory)
        self._prompts: Dict[str, str] = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted(self.directory.glob("*.md"))
        }
        missing = set(required) - set(self._prompts)
        if missing:
            raise RuntimeError(
                f"{self.directory}에 필요한 프롬프트가 없습니다: {sorted(missing)}"
            )

    def names(self) -> list[str]:
        return sorted(self._prompts)

    def prompt(self, name: str) -> str:
        return self._prompts[name]


class InMemoryPrompts:
    """테스트/실험용. 파일 없이 프롬프트를 직접 주입한다."""

    def __init__(self, prompts: Dict[str, str]):
        self._prompts = dict(prompts)

    def names(self) -> list[str]:
        return sorted(self._prompts)

    def prompt(self, name: str) -> str:
        return self._prompts[name]
