"""Capability catalog: the facts the Mapper may judge from (AV-007).

Skills hold the *method*; the catalog holds usage vocabulary, detect rules, grade rules by
target, known differences and standard tests. Capabilities outside it are never verified.
"""

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Target = Literal["AWS", "GCP", "ON_PREM"]


class CatalogError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DetectRule(_Model):
    source: Literal["MANIFEST", "CODE", "ENV", "SQL"]
    pattern: str

    @field_validator("pattern")
    @classmethod
    def valid_regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return value


class GradeRule(_Model):
    target: Target
    grade: Literal["DIRECT", "API_DIFF", "PARTIAL", "NONE"]
    when: str
    loses: list[str] = Field(default_factory=list)


class CatalogEntry(_Model):
    capability: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    title: str
    usage_vocabulary: list[str] = Field(min_length=1)
    detect_rules: list[DetectRule] = Field(min_length=1)
    grade_rules: list[GradeRule] = Field(min_length=1)
    known_differences: list[str]
    standard_tests: list[str]
    skill: str | None


class Catalog:
    def __init__(self, entries: list[CatalogEntry]) -> None:
        self.entries = sorted(entries, key=lambda e: e.capability)

    @property
    def capabilities(self) -> set[str]:
        return {e.capability for e in self.entries}

    def get(self, capability: str) -> CatalogEntry:
        for entry in self.entries:
            if entry.capability == capability:
                return entry
        raise KeyError(capability)

    def candidates(self, evidence_text: str) -> list[CatalogEntry]:
        return [
            e
            for e in self.entries
            if any(re.search(r.pattern, evidence_text, re.IGNORECASE) for r in e.detect_rules)
        ]


def load_catalog(directory: Path | str, *, skills_root: Path | str | None = None) -> Catalog:
    entries: list[CatalogEntry] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            entry = CatalogEntry.model_validate(yaml.safe_load(path.read_text()))
        except (yaml.YAMLError, ValidationError) as exc:
            raise CatalogError(
                f"{path.name}: invalid catalog entry ({type(exc).__name__})"
            ) from exc
        if entry.capability != path.stem:
            raise CatalogError(f"{path.name}: file name must match the capability")
        if (
            skills_root is not None
            and entry.skill
            and not (Path(skills_root) / entry.skill / "SKILL.md").is_file()
        ):
            raise CatalogError(f"{path.name}: skill '{entry.skill}' does not exist")
        entries.append(entry)
    names = [e.capability for e in entries]
    if len(names) != len(set(names)):
        raise CatalogError("duplicate capability in the catalog")
    return Catalog(entries)
