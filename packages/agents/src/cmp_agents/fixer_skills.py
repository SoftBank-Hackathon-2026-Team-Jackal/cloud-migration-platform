"""Which skill fits which failure, for the Fixer (ticket 31).

The Fixer does not get every skill in every run. A rule names a skill and the situation it is for:
the failure classes it applies to and patterns the failure's reason codes must match. A rule can be
added with the skill it belongs to (for example a storage skill that fits a blocked call to an
object store) without touching the Fixer. The skill text is not put in the prompt: the Fixer is told
the names of the skills that fit and reads one with `load_skill` when it needs it.

A rule may also name the targets it is for (`targets`: AWS, GCP, ON_PREM). That lets a skill be
split by target or not: one skill without `targets` serves every target and carries the target
differences in its own text, or several skills each limited to a target. Both can coexist, and one
failure may select more than one skill. A rule with `targets` never matches when the target is not
known.

Rules live next to the skills in `fixer-triggers.yaml`. A missing or broken file means no skill is
offered; the Fixer then works as before.
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

TRIGGERS_FILE = "fixer-triggers.yaml"
# The Fixer is called for these classes only (INFRA and RENDER are not fixable).
FIXABLE_CLASSES = frozenset({"SPEC", "CODE", "BUILD"})
# The Tester's skill stays with the Tester: the Fixer must not shape tests.
NOT_FOR_THE_FIXER = frozenset({"test-authoring"})


class SkillTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    # Empty means every class the Fixer is called for.
    failure_classes: list[Literal["SPEC", "CODE", "BUILD"]] = Field(default_factory=list)
    # Empty means every target. With targets, the rule matches only a known, listed target.
    targets: list[Literal["AWS", "GCP", "ON_PREM"]] = Field(default_factory=list)
    reason_patterns: list[str] = Field(min_length=1)

    @field_validator("reason_patterns")
    @classmethod
    def _compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    def matches(
        self, failure_class: str, reasons: Sequence[str], target_provider: str | None = None
    ) -> bool:
        if self.failure_classes and failure_class not in self.failure_classes:
            return False
        if self.targets and target_provider not in self.targets:
            return False
        return any(re.search(p, reason) for p in self.reason_patterns for reason in reasons)


def load_triggers(skills_root: Path | str | None) -> list[SkillTrigger]:
    """The rules next to the skills; no file means no rules. A broken file raises."""
    if skills_root is None:
        return []
    path = Path(skills_root) / TRIGGERS_FILE
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    return [SkillTrigger.model_validate(item) for item in raw.get("triggers", [])]


def select_skills(
    skills_root: Path | str | None,
    failure_class: str,
    reasons: Sequence[str],
    target_provider: str | None = None,
) -> list[str]:
    """Skill names that fit this failure (and target, when the rule names one), once each, in rule
    order. Fails closed: when the rules cannot be read, no skill is offered."""
    if skills_root is None or failure_class not in FIXABLE_CLASSES:
        return []
    try:
        triggers = load_triggers(skills_root)
    except (OSError, yaml.YAMLError, ValidationError, ValueError, re.error):
        return []
    chosen: list[str] = []
    for trigger in triggers:
        if trigger.skill in NOT_FOR_THE_FIXER or trigger.skill in chosen:
            continue
        if not (Path(skills_root) / trigger.skill / "SKILL.md").is_file():
            continue
        if trigger.matches(failure_class, reasons, target_provider):
            chosen.append(trigger.skill)
    return chosen
