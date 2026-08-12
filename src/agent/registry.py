"""
Skill Registry.

skills/ 폴더를 스캔해 SKILL.md를 읽는다.
frontmatter(manifest)만 즉시 파싱하고, 본문(instructions)은 lazy load한다.

progressive disclosure:
플래너는 name + description + requires/provides만 본다. skill이 30개여도
플래너 프롬프트는 30줄이다. 본문 전체를 매번 올리면 컨텍스트가 금방 터진다.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .contract import SkillManifest


@dataclass
class LoadedSkill:
    manifest: SkillManifest
    path: Path
    _body: Optional[str] = None
    _handler = None

    @property
    def instructions(self) -> str:
        """SKILL.md 본문. 선택된 뒤에야 읽는다."""
        if self._body is None:
            self._body = _split_frontmatter(self.path.joinpath("SKILL.md").read_text())[1].strip()
        return self._body

    @property
    def handler(self):
        """handler.py의 run(). 없으면 None -> executor가 실패로 처리한다."""
        if self._handler is None:
            hp = self.path / "handler.py"
            if not hp.exists():
                return None
            mod_name = f"skill_{self.manifest.name.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(mod_name, hp)
            module = importlib.util.module_from_spec(spec)
            # sys.modules 등록은 필수다. 등록하지 않으면 handler가 정의한 Pydantic 모델이
            # `from __future__ import annotations` 아래에서 타입 문자열을 해석할
            # 네임스페이스를 찾지 못해 "is not fully defined" 오류로 전부 실패한다.
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            self._handler = getattr(module, "run", None)
        return self._handler

    def index_line(self) -> str:
        m = self.manifest
        return (
            f"- {m.name}: {m.description} "
            f"(requires={[s.value for s in m.requires]}, provides={[s.value for s in m.provides]}, cost={m.cost.value})"
        )


def _split_frontmatter(text: str):
    if not text.startswith("---"):
        raise ValueError("SKILL.md는 YAML frontmatter로 시작해야 합니다")
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm), body


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._skills: Dict[str, LoadedSkill] = {}
        self.errors: List[str] = []
        self.reload()

    def reload(self) -> None:
        self._skills.clear()
        self.errors.clear()
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            md = d / "SKILL.md"
            if not md.exists():
                continue
            try:
                fm, _ = _split_frontmatter(md.read_text())
                manifest = SkillManifest.model_validate(fm)
            except Exception as exc:  # noqa: BLE001
                # 잘못된 skill 하나가 전체 로딩을 막지 않게 한다.
                self.errors.append(f"{d.name}: {exc}")
                continue
            self._skills[manifest.name] = LoadedSkill(manifest=manifest, path=d)

    def all(self) -> List[LoadedSkill]:
        return list(self._skills.values())

    def get(self, name: str) -> LoadedSkill:
        return self._skills[name]

    def index(self) -> str:
        """플래너 프롬프트에 넣는 skill 목록."""
        return "\n".join(s.index_line() for s in self.all())
