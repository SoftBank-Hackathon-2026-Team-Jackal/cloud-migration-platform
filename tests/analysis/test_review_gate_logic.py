import pytest
from cmp_contracts.analysis import Evidence, MappingRow
from cmp_worker.review_gate import ReviewGate

REF = {"id": "analysis", "version": "1"}


def row(id, grade, proposed=True):
    return MappingRow(
        id=id, capability="cap-" + id, usage="usage " + id, grade=grade,
        required_proposed=proposed, required_reason="why", required=None,
        disposition="MIGRATE", loses=["something"], treatment=None, status="EVIDENCE_CHECKED",
        evidence=[Evidence(kind="CODE", locator="a.go:1", verified=True)],
    )  # fmt: skip


def rows():
    return [row("a", "DIRECT"), row("b", "PARTIAL"), row("c", "NONE")]


def confirm(required, ref=REF, actor="alice", **extra):
    return {"analysis_ref": ref, "required": required, "actor": actor, **extra}


def approve(approvals, ref=REF, actor="alice"):
    return {"analysis_ref": ref, "approvals": approvals, "actor": actor}


def codes(gate):
    return [r["code"] for r in gate.rejections]


def test_a_new_gate_waits_for_confirmation_with_empty_treatments():
    gate = ReviewGate(REF, rows())
    assert gate.phase == "WAITING_CONFIRMATION" and not gate.finished
    assert all(r.treatment is None and r.required is None for r in gate.rows)
    view = gate.view()
    assert view["pending"] == "confirm_requirements"
    assert [i["id"] for i in view["items"]] == ["a", "b", "c"]
    assert view["items"][1]["required_proposed"] is True and view["items"][1]["loses"] == [
        "something"
    ]


def test_confirming_fills_treatments_and_waits_for_loss_approval():
    gate = ReviewGate(REF, rows())
    assert gate.confirm(confirm({"a": True, "b": False, "c": False}, operational=["backup"]))
    assert gate.phase == "WAITING_APPROVAL"
    assert [r.treatment for r in gate.rows] == ["PROCEED", "APPROVAL", "APPROVAL"]
    assert gate.view()["pending"] == "approve_losses"


def test_approving_every_loss_finishes_the_gate_and_keeps_the_decisions():
    gate = ReviewGate(REF, rows())
    gate.confirm(
        confirm({"a": True, "b": False, "c": False}, operational=["backup"], actor="alice")
    )
    assert gate.approve(approve({"b": "APPROVED", "c": "APPROVED"}, actor="bob"))
    assert gate.phase == "APPROVED" and gate.finished
    decisions = gate.decisions()
    assert decisions == {
        "required": {"a": True, "b": False, "c": False},
        "operational": ["backup"],
        "approvals": {"b": "APPROVED", "c": "APPROVED"},
        "actor": "bob",
    }


def test_no_losses_means_no_approval_step():
    gate = ReviewGate(REF, [row("a", "DIRECT"), row("b", "API_DIFF")])
    gate.confirm(confirm({"a": True, "b": True}))
    assert gate.phase == "APPROVED"


def test_a_required_feature_that_cannot_be_kept_blocks_with_reason_and_alternative():
    gate = ReviewGate(REF, rows())
    gate.confirm(confirm({"a": True, "b": False, "c": True}))
    assert gate.phase == "BLOCKED" and gate.finished
    (blocked,) = gate.blocked()
    assert blocked["id"] == "c" and blocked["reason"] and blocked["alternative"]


def test_rejecting_a_loss_makes_the_feature_required_and_recomputes_to_block():
    gate = ReviewGate(REF, rows())
    gate.confirm(confirm({"a": True, "b": False, "c": False}))
    gate.approve(approve({"b": "REJECTED", "c": "APPROVED"}))
    assert gate.phase == "BLOCKED"
    assert [b["id"] for b in gate.blocked()] == ["b"]
    assert gate.decisions()["required"]["b"] is True


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            confirm({"a": True, "b": True, "c": True}, ref={"id": "analysis", "version": "2"}),
            "ANALYSIS_REF_MISMATCH",
        ),
        (confirm({"a": True, "b": True}), "MISSING_ROW"),
        (confirm({"a": True, "b": True, "c": True, "zzz": True}), "UNKNOWN_ROW"),
        (confirm({"a": "yes", "b": True, "c": True}), "INVALID_PAYLOAD"),
        ({"required": {}}, "INVALID_PAYLOAD"),
    ],
)
def test_bad_confirmations_are_rejected_and_the_gate_keeps_waiting(payload, code):
    gate = ReviewGate(REF, rows())
    assert gate.confirm(payload) is False
    assert gate.phase == "WAITING_CONFIRMATION" and codes(gate) == [code]
    assert all(r.treatment is None for r in gate.rows)


@pytest.mark.parametrize(
    ("approvals", "code"),
    [
        ({"b": "APPROVED"}, "MISSING_ROW"),
        ({"b": "APPROVED", "c": "APPROVED", "a": "APPROVED"}, "NOT_APPROVABLE"),
        ({"b": "APPROVED", "c": "APPROVED", "zzz": "APPROVED"}, "UNKNOWN_ROW"),
        ({"b": "MAYBE", "c": "APPROVED"}, "INVALID_PAYLOAD"),
    ],
)
def test_bad_approvals_are_rejected(approvals, code):
    gate = ReviewGate(REF, rows())
    gate.confirm(confirm({"a": True, "b": False, "c": False}))
    assert gate.approve(approve(approvals)) is False
    assert gate.phase == "WAITING_APPROVAL" and codes(gate)[-1] == code


def test_signals_in_the_wrong_phase_are_rejected():
    gate = ReviewGate(REF, rows())
    assert gate.approve(approve({"b": "APPROVED"})) is False
    gate.confirm(confirm({"a": True, "b": False, "c": False}))
    assert gate.confirm(confirm({"a": True, "b": True, "c": True})) is False
    gate.approve(approve({"b": "APPROVED", "c": "APPROVED"}))
    assert gate.approve(approve({"b": "REJECTED", "c": "APPROVED"})) is False
    assert codes(gate) == ["NOT_WAITING", "NOT_WAITING", "NOT_WAITING"]
    assert gate.phase == "APPROVED"


def test_rejection_records_carry_no_signal_contents():
    gate = ReviewGate(REF, rows())
    gate.confirm(confirm({"a": "secret-ish-value", "b": True, "c": True}, actor="alice"))
    assert "secret-ish-value" not in str(gate.rejections)
