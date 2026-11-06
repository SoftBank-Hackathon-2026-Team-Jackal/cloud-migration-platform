"""Required-feature confirmation and loss approval (AV-022) as a pure state machine.

The analysis child Workflow feeds Signals into the gate and waits until it is finished.
Nothing here reads a clock or does I/O, so it is safe inside a Workflow. There is no
automatic approval: the gate only moves when a valid decision from a person arrives.
"""

from typing import Any

from cmp_agents.mapping import compute_treatments
from cmp_contracts.analysis import (
    ApproveLossesSignal,
    ConfirmRequirementsSignal,
    MappingRow,
    SecurityJudgement,
)
from cmp_scanners.security_judge import PROOF
from pydantic import ValidationError

# A security judgement is approved or rejected under this id, next to the mapping rows' ids.
SECURITY_PREFIX = "security:"

WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
WAITING_APPROVAL = "WAITING_APPROVAL"
APPROVED = "APPROVED"
BLOCKED = "BLOCKED"

_ALTERNATIVES = {
    "NONE": "대상에서 대체할 수 없는 기능이다. 기능을 쓰지 않도록 코드를 고치거나 다른 대상을 검토한다.",
    "PARTIAL": "대상에서 일부만 지원한다. 대체 방법을 증명하거나 필수 여부를 다시 검토한다.",
}


class ReviewGate:
    def __init__(
        self,
        analysis_ref: dict[str, str],
        rows: list[dict[str, Any] | MappingRow],
        security: list[dict[str, Any] | SecurityJudgement] | None = None,
    ):
        self.analysis_ref = dict(analysis_ref)
        self.rows = [r if isinstance(r, MappingRow) else MappingRow.model_validate(r) for r in rows]
        # Ticket 29-B: only a LOWER judgement asks for an answer; the rest is shown, never asked.
        self.security = [
            j if isinstance(j, SecurityJudgement) else SecurityJudgement.model_validate(j)
            for j in security or []
        ]
        self._security_ids = {f"{SECURITY_PREFIX}{j.criterion}" for j in self.security}
        self._security_lower = {
            f"{SECURITY_PREFIX}{j.criterion}" for j in self.security if j.verdict == "LOWER"
        }
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
        known = {r.id for r in self.rows} | self._security_ids
        approvable = self._approvable()
        if set(signal.approvals) - known:
            return self._reject(name, "UNKNOWN_ROW")
        if set(signal.approvals) - approvable:
            return self._reject(name, "NOT_APPROVABLE")
        if approvable - set(signal.approvals):
            return self._reject(name, "MISSING_ROW")
        self._approvals = dict(signal.approvals)
        self._actor = signal.actor
        for row_id, decision in signal.approvals.items():
            if decision == "REJECTED" and row_id not in self._security_ids:
                self._required[row_id] = True  # a rejected loss means the feature is required
        self._recompute()
        return True

    def _approvable(self) -> set[str]:
        return {r.id for r in self.rows if r.treatment == "APPROVAL"} | self._security_lower

    def _security_rejected(self) -> list[str]:
        return sorted(i for i in self._security_lower if self._approvals.get(i) == "REJECTED")

    def _recompute(self) -> None:
        self.rows = compute_treatments(self.rows, self._required)
        treatments = {r.treatment for r in self.rows}
        if "BLOCK" in treatments or self._security_rejected():
            self.phase = BLOCKED  # refusing a lower security level stops the move
        elif ("APPROVAL" in treatments or self._security_lower) and not self._approvals:
            self.phase = WAITING_APPROVAL
        else:
            self.phase = APPROVED

    # -- reports -----------------------------------------------------------------

    def blocked(self) -> list[dict[str, str]]:
        refused = {f"{SECURITY_PREFIX}{j.criterion}": j for j in self.security}
        lost_security = [
            {
                "id": item,
                "capability": "security",
                "usage": refused[item].criterion,
                "grade": "LOWER",
                "reason": f"SECURITY_LOWER: {refused[item].criterion}",
                "alternative": refused[item].suggestion or "",
            }
            for item in self._security_rejected()
        ]
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
        ] + lost_security

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
            "security": [
                {
                    "id": f"{SECURITY_PREFIX}{j.criterion}",
                    "criterion": j.criterion,
                    "source_state": j.source_state,
                    "target_state": j.target_state,
                    "verdict": j.verdict,
                    "weak_source": j.weak_source,
                    "reasons": list(j.reasons),
                    "suggestion": j.suggestion,
                    "proof": PROOF.get(j.criterion),
                    "approvable": j.verdict == "LOWER",
                    "decision": self._approvals.get(f"{SECURITY_PREFIX}{j.criterion}"),
                }
                for j in self.security
            ],
            "blocked": self.blocked(),
            "rejections": list(self.rejections),
        }


class ReapprovalGate(ReviewGate):
    """Asks again, for changed rows only, after the Sandbox loop changed how they are handled.

    Earlier decisions (`required`, actor) are carried in, so only the approval step remains.
    Every changed row needs an explicit answer, whatever its treatment is. A change that turns
    into BLOCK ends the gate as BLOCKED. Same Signal contract and rejection records as the
    first review.
    """

    def __init__(
        self,
        analysis_ref: dict[str, str],
        rows: list[dict[str, Any] | MappingRow],
        *,
        required: dict[str, bool],
        actor: str,
    ):
        super().__init__(analysis_ref, rows)
        self._required = {r.id: bool(required.get(r.id, False)) for r in self.rows}
        self._actor = actor
        self.rows = compute_treatments(self.rows, self._required)
        self.phase = BLOCKED if any(r.treatment == "BLOCK" for r in self.rows) else WAITING_APPROVAL
        if not self.rows:
            self.phase = APPROVED

    def _approvable(self) -> set[str]:
        return {r.id for r in self.rows}

    def _recompute(self) -> None:
        for row_id, decision in self._approvals.items():
            if decision == "REJECTED":
                self._required[row_id] = True
        self.rows = compute_treatments(self.rows, self._required)
        if any(r.treatment == "BLOCK" for r in self.rows):
            self.phase = BLOCKED
        else:
            self.phase = APPROVED
