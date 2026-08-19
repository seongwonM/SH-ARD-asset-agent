"""skill 본문(markdown) 로더.

skill은 코드가 아니라 프롬프트다. `skills/<name>.md` 파일 하나가 skill 하나이고,
파일 내용이 그대로 LLM system 프롬프트가 된다. 등록 절차는 없다 - 폴더를 읽는다.

파이프라인은 `SkillLibrary` 프로토콜(이름 -> 프롬프트 문자열)에만 의존하므로,
테스트에서는 dict 하나로 대체할 수 있다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Protocol


class SkillLibrary(Protocol):
    def prompt(self, name: str) -> str:
        """skill 본문을 돌려준다. 없으면 KeyError."""


class FileSystemSkillLibrary:
    def __init__(self, skill_dir: Path, required: Iterable[str] = ()):
        self.skill_dir = Path(skill_dir)
        self._prompts: Dict[str, str] = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted(self.skill_dir.glob("*.md"))
        }
        missing = set(required) - set(self._prompts)
        if missing:
            raise RuntimeError(
                f"{self.skill_dir}에 필요한 skill이 없습니다: {sorted(missing)}"
            )

    def names(self) -> list[str]:
        return sorted(self._prompts)

    def prompt(self, name: str) -> str:
        return self._prompts[name]


class InMemorySkillLibrary:
    """테스트/실험용. 파일 없이 프롬프트를 직접 주입한다."""

    def __init__(self, prompts: Dict[str, str]):
        self._prompts = dict(prompts)

    def names(self) -> list[str]:
        return sorted(self._prompts)

    def prompt(self, name: str) -> str:
        return self._prompts[name]
