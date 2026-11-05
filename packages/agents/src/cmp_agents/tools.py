"""Allow-listed tools. Shell, network and credential tools can never be registered."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

_FORBIDDEN = re.compile(
    r"shell|bash|exec|subprocess|run_command|command|http|request|fetch|curl|wget|socket|"
    r"network|credential|secret|token|password|aws|boto|gcloud|kubectl",
    re.IGNORECASE,
)


class ForbiddenToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        if _FORBIDDEN.search(tool.name):
            raise ForbiddenToolError(f"tool '{tool.name}' is not an allowed kind of tool")
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())
