"""Required-feature confirmation and loss approval (AV-022) as a pure state machine.

The analysis child Workflow feeds Signals into the gate and waits until it is finished.
Nothing here reads a clock or does I/O, so it is safe inside a Workflow. There is no
automatic approval: the gate only moves when a valid decision from a person arrives.
"""

from typing import Any

from cmp_agents.mapping import compute_treatments
from cmp_contracts.analysis import ApproveLossesSignal, ConfirmRequirementsSignal, MappingRow
from pydantic import ValidationError

WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
WAITING_APPROVAL = "WAITING_APPROVAL"
APPROVED = "APPROVED"
BLOCKED = "BLOCKED"

_ALTERNATIVES = {
    "NONE": "대상에서 대체할 수 없는 기능이다. 기능을 쓰지 않도록 코드를 고치거나 다른 대상을 검토한다.",
    "PARTIAL": "대상에서 일부만 지원한다. 대체 방법을 증명하거나 필수 여부를 다시 검토한다.",
}


class ReviewGate:
    def __init__(self, analysis_ref: dict[str, str], rows: list[dict[str, Any] | MappingRow]):
        self.analysis_ref = dict(analysis_ref)
        self.rows = [r if isinstance(r, MappingRow) else MappingRow.model_validate(r) for r in rows]
        self.phase = WAITING_CONFIRMATION
        self.rejections: list[dict[str, str]] = []
        self._required: dict[str, bool] = {}
        self._operational: list[str] = []
        self._approvals: dict[str, str] = {}
        self._actor = ""

    @property
    def finished(self) -> bool:
        return self.phase in {APPROVED, BLOCKED}

    # -- signals -----------------------------------------------------------------

    def _reject(self, signal: str, code: str, detail: str = "") -> bool:
        self.rejections.append({"signal": signal, "code": code, "detail": detail})
        return False

    def confirm(self, payload: dict[str, Any]) -> bool:
        name = "confirm_requirements"
        if self.phase != WAITING_CONFIRMATION:
            return self._reject(name, "NOT_WAITING", self.phase)
        try:
            signal = ConfirmRequirementsSignal.model_validate(payload)
        except ValidationError:
            return self._reject(name, "INVALID_PAYLOAD")
        if signal.analysis_ref.model_dump() != self.analysis_ref:
            return self._reject(name, "ANALYSIS_REF_MISMATCH")
        known = {r.id for r in self.rows}
        if set(signal.required) - known:
            return self._reject(name, "UNKNOWN_ROW")
        if known - set(signal.required):
            return self._reject(name, "MISSING_ROW")
        self._required = dict(signal.required)
        self._operational = list(signal.operational)
        self._actor = signal.actor
        self._recompute()
        return True

    def approve(self, payload: dict[str, Any]) -> bool:
        name = "approve_losses"
        if self.phase != WAITING_APPROVAL:
            return self._reject(name, "NOT_WAITING", self.phase)
        try:
            signal = ApproveLossesSignal.model_validate(payload)
        except ValidationError:
            return self._reject(name, "INVALID_PAYLOAD")
        if signal.analysis_ref.model_dump() != self.analysis_ref:
            return self._reject(name, "ANALYSIS_REF_MISMATCH")
        known = {r.id for r in self.rows}
        approvable = {r.id for r in self.rows if r.treatment == "APPROVAL"}
        if set(signal.approvals) - known:
            return self._reject(name, "UNKNOWN_ROW")
        if set(signal.approvals) - approvable:
            return self._reject(name, "NOT_APPROVABLE")
        if approvable - set(signal.approvals):
            return self._reject(name, "MISSING_ROW")
        self._approvals = dict(signal.approvals)
        self._actor = signal.actor
        for row_id, decision in signal.approvals.items():
            if decision == "REJECTED":
                self._required[row_id] = True  # a rejected loss means the feature is required
        self._recompute()
        return True

    def _recompute(self) -> None:
        self.rows = compute_treatments(self.rows, self._required)
        treatments = {r.treatment for r in self.rows}
        if "BLOCK" in treatments:
            self.phase = BLOCKED
        elif "APPROVAL" in treatments and not self._approvals:
            self.phase = WAITING_APPROVAL
        else:
            self.phase = APPROVED

    # -- reports -----------------------------------------------------------------

    def blocked(self) -> list[dict[str, str]]:
        return [
            {
                "id": r.id,
                "capability": r.capability,
                "usage": r.usage,
                "grade": r.grade,
                "reason": f"필수 기능이 {r.grade} 등급이라 이전할 수 없다.",
                "alternative": _ALTERNATIVES.get(r.grade, ""),
            }
            for r in self.rows
            if r.treatment == "BLOCK"
        ]

    def decisions(self) -> dict[str, Any]:
        return {
            "required": dict(self._required),
            "operational": list(self._operational),
            "approvals": dict(self._approvals),
            "actor": self._actor,
        }

    def view(self) -> dict[str, Any]:
        pending = {WAITING_CONFIRMATION: "confirm_requirements", WAITING_APPROVAL: "approve_losses"}
        return {
            "phase": self.phase,
            "analysis_ref": dict(self.analysis_ref),
            "pending": pending.get(self.phase),
            "items": [
                {
                    "id": r.id,
                    "capability": r.capability,
                    "usage": r.usage,
                    "grade": r.grade,
                    "loses": list(r.loses),
                    "required_proposed": r.required_proposed,
                    "required_reason": r.required_reason,
                    "required": r.required,
                    "treatment": r.treatment,
                }
                for r in self.rows
            ],
            "blocked": self.blocked(),
            "rejections": list(self.rejections),
        }
