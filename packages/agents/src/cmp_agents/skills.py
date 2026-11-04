"""Skill loader: only `skills/<name>/SKILL.md` for allowed names; examples on request."""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cmp_agents.tools import ToolSpec

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SkillNotFoundError(Exception):
    pass


class SkillLoader:
    def __init__(self, root: Path | str, allowed: list[str]) -> None:
        self._root = Path(root)
        self._allowed = set(allowed)
        self.loaded: list[str] = []

    def load(self, name: str, part: Literal["skill", "examples"] = "skill") -> str:
        if not _NAME.fullmatch(name) or name not in self._allowed:
            raise SkillNotFoundError(f"skill '{name}' is not available")
        directory = self._root / name
        if part == "skill":
            path = directory / "SKILL.md"
            if not path.is_file():
                raise SkillNotFoundError(f"skill '{name}' has no SKILL.md")
            text = path.read_text()
        else:
            files = sorted((directory / "examples").glob("*.md"))
            if not files:
                raise SkillNotFoundError(f"skill '{name}' has no examples")
            text = "\n\n".join(f.read_text() for f in files)
        if name not in self.loaded:
            self.loaded.append(name)
        return text


class _LoadSkillArgs(BaseModel):
    name: str
    part: Literal["skill", "examples"] = "skill"


def make_load_skill_tool(loader: SkillLoader) -> ToolSpec:
    def handler(args: _LoadSkillArgs) -> str:
        try:
            return loader.load(args.name, args.part)
        except SkillNotFoundError as exc:
            return f"error: {exc}"

    return ToolSpec(
        name="load_skill",
        description="Read a skill's guidance (part='skill') or worked examples (part='examples').",
        input_model=_LoadSkillArgs,
        handler=handler,
    )
