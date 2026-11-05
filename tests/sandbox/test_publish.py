import json
import subprocess

import pytest
from cmp_contracts.analysis import AnalysisReport, MappingRow
from cmp_contracts.provisioning import VersionRef
from cmp_testsuite import Expect, HttpStep, Suite, SuiteCase
from cmp_worker.final_report import (
    FinalReportError,
    apply_row_changes,
    build_final_report,
    changed_rows,
    final_version,
    mark_verified,
    with_decisions,
)
from cmp_worker.fix_patch import (
    PatchError,
    PreparedOnlyPublisher,
    apply_patch_tree_sha256,
    branch_name,
    build_patch_artifact,
    make_patch,
    tree_sha256,
)
from cmp_worker.review_gate import APPROVED, BLOCKED, WAITING_APPROVAL, ReapprovalGate


def row(
    row_id,
    capability="postgres",
    *,
    grade="DIRECT",
    disposition="MIGRATE",
    status="EVIDENCE_CHECKED",
):
    return MappingRow(
        id=row_id, capability=capability, usage=f"{capability} use", grade=grade,
        required_proposed=True, required_reason="r", required=None, disposition=disposition,
        loses=[], treatment=None, status=status, evidence=[],
    )  # fmt: skip


REF = VersionRef(id="analysis", version="v1")
REQUIRED = {"r1": True, "r2": False, "r3": False}


def rows():
    return with_decisions(
        [
            row("r1"),
            row("r2", "ingress", grade="PARTIAL"),
            row("r3", "object-storage", disposition="KEEP"),
        ],
        REQUIRED,
    )


def case(case_id, covers):
    return SuiteCase(
        id=case_id, source="STANDARD", effect="READ_ONLY", covers=covers,
        steps=[HttpStep(path="/x", expect=Expect(status=200))],
    )  # fmt: skip


# ----------------------------------------------------------------- final report


def test_a_row_is_verified_only_when_a_passed_test_declares_it_and_its_evidence_was_checked():
    suite = Suite(
        name="d",
        cases=[case("pg", ["postgres"]), case("ing", ["ingress"]), case("s3", ["object-storage"])],
    )
    base = [*rows(), row("r4", "queue", status="UNVERIFIED")]
    results = [
        {"id": "pg", "status": "PASSED"},
        {"id": "ing", "status": "FAILED"},
        {"id": "s3", "status": "SKIPPED"},
    ]
    status = {r.id: r.status for r in mark_verified(base, suite, results)}
    assert status == {
        "r1": "SANDBOX_VERIFIED",
        "r2": "EVIDENCE_CHECKED",
        "r3": "EVIDENCE_CHECKED",
        "r4": "UNVERIFIED",
    }
    unverified = mark_verified([row("r9", "postgres", status="UNVERIFIED")], suite, results)
    assert unverified[0].status == "UNVERIFIED"


def test_only_rows_whose_handling_changed_need_a_new_answer():
    before = rows()
    after = with_decisions(apply_row_changes(before, {"r2": "KEEP"}), REQUIRED)
    assert [r.id for r in changed_rows(before, after)] == ["r2"]
    assert changed_rows(before, before) == []


def test_a_change_to_an_unknown_row_is_refused():
    with pytest.raises(FinalReportError) as exc:
        apply_row_changes(rows(), {"nope": "KEEP"})
    assert exc.value.code == "UNKNOWN_ROW"


def test_the_final_version_is_a_hash_of_the_content():
    one = final_version(REF, "a" * 64, rows())
    assert one == final_version(REF, "a" * 64, rows())
    assert one != final_version(REF, "b" * 64, rows()) and one.version != REF.version
    assert one != final_version(REF, "a" * 64, apply_row_changes(rows(), {"r2": "KEEP"}))


def test_the_final_report_keeps_everything_but_rows_requirements_and_the_version():
    report = AnalysisReport(
        migration_id="m", ref=REF, base_commit="c" * 40, commit_verified=False,
        usage_profile={"usages": []}, mapping=rows(), precheck={"findings": []},
        external_entries=[], source_cidrs=["10.0.0.0/16"], source_db_images={}, unmapped_items=[], unverified_risks=["R"],
    )  # fmt: skip
    final = build_final_report(
        report, [r.model_copy(update={"status": "SANDBOX_VERIFIED"}) for r in rows()], "a" * 64
    )
    assert final.ref.version != "v1" and final.base_commit == report.base_commit
    assert final.source_cidrs == ["10.0.0.0/16"] and final.unverified_risks == ["R"]
    assert {r.status for r in final.mapping} == {"SANDBOX_VERIFIED"}


# ------------------------------------------------------------------ re-approval


def gate(changed):
    return ReapprovalGate(
        {"id": "analysis", "version": "v2"}, changed, required=REQUIRED, actor="seungcheol"
    )


def approve(g, **decisions):
    return g.approve(
        {
            "analysis_ref": {"id": "analysis", "version": "v2"},
            "approvals": decisions,
            "actor": "park",
        }
    )


def test_changed_rows_are_asked_again_even_when_they_would_proceed():
    g = gate([row("r1")])
    assert g.phase == WAITING_APPROVAL and not g.finished
    assert approve(g, r1="APPROVED") and g.phase == APPROVED
    assert g.decisions()["approvals"] == {"r1": "APPROVED"} and g.decisions()["required"] == {
        "r1": True
    }


def test_every_changed_row_needs_an_answer_and_other_rows_are_not_asked():
    g = gate([row("r1"), row("r2", "ingress", grade="PARTIAL")])
    assert not approve(g, r1="APPROVED") and g.rejections[-1]["code"] == "MISSING_ROW"
    assert (
        not approve(g, r1="APPROVED", r2="APPROVED", r3="APPROVED")
        and g.rejections[-1]["code"] == "UNKNOWN_ROW"
    )
    assert g.phase == WAITING_APPROVAL


def test_an_answer_for_another_analysis_version_is_refused_and_nothing_approves_itself():
    g = gate([row("r1")])
    assert not g.approve(
        {
            "analysis_ref": {"id": "analysis", "version": "OLD"},
            "approvals": {"r1": "APPROVED"},
            "actor": "x",
        }
    )
    assert g.rejections[-1]["code"] == "ANALYSIS_REF_MISMATCH" and g.phase == WAITING_APPROVAL


def test_rejecting_a_lossy_change_turns_the_feature_into_a_required_one_and_blocks():
    g = gate([row("r2", "ingress", grade="PARTIAL")])
    assert approve(g, r2="REJECTED") and g.phase == BLOCKED
    assert g.blocked()[0]["id"] == "r2"


def test_a_changed_required_row_that_cannot_be_migrated_blocks_at_once():
    g = ReapprovalGate(
        {"id": "a", "version": "v"}, [row("r1", grade="NONE")], required={"r1": True}, actor="x"
    )
    assert g.phase == BLOCKED and g.finished


def test_nothing_changed_means_nothing_to_ask():
    assert gate([]).phase == APPROVED


# ----------------------------------------------------------------------- patch


def tree(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return path


def test_the_tree_hash_ignores_git_and_follows_content(tmp_path):
    a = tree(tmp_path / "a", {"main.go": "package main\n", "pkg/x.go": "package pkg\n"})
    b = tree(
        tmp_path / "b",
        {"pkg/x.go": "package pkg\n", "main.go": "package main\n", ".git/HEAD": "ref"},
    )
    assert tree_sha256(a) == tree_sha256(b)
    (b / "main.go").write_text("package main // changed\n")
    assert tree_sha256(a) != tree_sha256(b)


def test_a_patch_reproduces_the_passed_source_from_the_base(tmp_path):
    base = tree(
        tmp_path / "base",
        {"main.go": "package main\nvar port = 8080\n", "keep.txt": "same\n", "old.go": "x\n"},
    )
    result = tree(
        tmp_path / "result",
        {
            "main.go": "package main\nvar port = 9898\n",
            "keep.txt": "same\n",
            "new/file.go": "package n\n",
        },
    )
    artifact = build_patch_artifact(base, result, migration_id="mig-1", reasons=["listen on 9898"])
    assert artifact.result_tree_sha256 == tree_sha256(
        result
    ) and artifact.base_tree_sha256 == tree_sha256(base)
    assert apply_patch_tree_sha256(base, artifact.patch) == tree_sha256(result)
    text = artifact.patch.decode()
    assert "a/main.go" in text and "+var port = 9898" in text and "deleted file mode" in text


def test_no_code_change_means_no_patch(tmp_path):
    base = tree(tmp_path / "base", {"main.go": "package main\n"})
    same = tree(tmp_path / "same", {"main.go": "package main\n"})
    assert (
        make_patch(base, same) == b""
        and build_patch_artifact(base, same, migration_id="m", reasons=[]) is None
    )


def test_a_patch_that_would_not_reproduce_the_result_is_never_offered(tmp_path, monkeypatch):
    base = tree(tmp_path / "base", {"a.go": "1\n"})
    result = tree(tmp_path / "result", {"a.go": "2\n"})
    monkeypatch.setattr("cmp_worker.fix_patch.apply_patch_tree_sha256", lambda *_: "0" * 64)
    with pytest.raises(PatchError) as exc:
        build_patch_artifact(base, result, migration_id="m", reasons=[])
    assert exc.value.code == "PATCH_DOES_NOT_REPRODUCE_RESULT"


def test_the_patch_text_carries_no_credentials_and_nothing_is_sent_anywhere(tmp_path):
    base = tree(tmp_path / "base", {"a.go": "1\n"})
    result = tree(tmp_path / "result", {"a.go": "2\n"})
    artifact = build_patch_artifact(
        base, result, migration_id="m", reasons=["used key AKIAABCDEFGHIJKLMNOP to call"]
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in artifact.body
    published = PreparedOnlyPublisher().publish(artifact, {"id": "p"})
    assert published == {"status": "PREPARED", "branch": artifact.branch}


def test_the_branch_name_is_a_safe_git_ref():
    name = branch_name("mig 1; rm -rf /")
    assert name.startswith("cmp/") and " " not in name and ";" not in name
    done = subprocess.run(
        ["git", "check-ref-format", "--branch", name], capture_output=True, check=False
    )
    assert done.returncode == 0, json.dumps(name)
