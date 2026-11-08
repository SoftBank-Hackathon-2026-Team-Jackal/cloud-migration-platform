from datetime import UTC, datetime

import pytest
from cmp_agents.llm import FakeLLMClient, LLMResponse, call
from cmp_contracts.analysis import AnalysisReport, ReviewRecord
from cmp_contracts.provisioning import Image
from cmp_contracts.verification import canonical_requirements
from cmp_testsuite import Suite, dump_bundle
from cmp_worker.activities.sandbox_loop import SandboxInput, SandboxLoopActivities
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.fix_patch import tree_sha256
from temporalio.exceptions import ApplicationError

from .test_deploy import FakeRunner, make_service, requirements
from .test_publish import REF, REQUIRED, case, rows

PG = Image(uri="docker.io/library/postgres@sha256:" + "a" * 64, platforms=["linux/amd64"])


class Recorder:
    def __init__(self):
        self.sent = []

    def publish(self, artifact, patch_ref):
        self.sent.append(artifact.branch)
        return {"status": "PREPARED", "branch": artifact.branch}


def world(
    tmp_path, *, with_analysis=True, llm=None, target_provider=None, security=None, judgements=None
):
    store = LocalArtifactStore(tmp_path / "store")
    base = tmp_path / "base"
    base.mkdir()
    (base / "main.go").write_text("package main\nvar port = 8080\n")
    reqs = requirements()
    req_ref = store.put(canonical_requirements(reqs)[0], ref_id="requirements", kind="requirements")
    suite = Suite(name="d", cases=[case("pg", ["postgres"]), case("ing", ["ingress"])])
    suite_ref = store.put(dump_bundle(suite), ref_id="suite", kind="test-suite")
    extra = {}
    if with_analysis:
        report = AnalysisReport(
            migration_id="mig-1", ref=REF, base_commit="c" * 40, commit_verified=False,
            usage_profile={"usages": []}, mapping=rows(), precheck={"findings": []},
            external_entries=[], source_cidrs=[], source_db_images={}, unmapped_items=[], unverified_risks=[],
            target_provider=target_provider, security=security, security_judgements=judgements,
        )  # fmt: skip
        record = ReviewRecord(
            analysis_ref=REF,
            required=REQUIRED,
            operational=[],
            approvals={"r2": "APPROVED"},
            actor="park",
            decided_at=datetime(2026, 11, 5, tzinfo=UTC),
        )
        extra = {
            "analysis_report_ref": store.put_json(
                report, ref_id="a", kind="analysis-report"
            ).model_dump(),
            "review_ref": store.put_json(record, ref_id="r", kind="review-record").model_dump(),
        }
    data = SandboxInput(
        migration_id="mig-1", run_id="run1", requirements_ref=req_ref, suite_ref=suite_ref,
        source={"workspace": str(base)}, database_image=PG, **extra,
    )  # fmt: skip
    recorder = Recorder()
    activities = SandboxLoopActivities(
        service=make_service(FakeRunner()), store=store, workspace_root=tmp_path / "work",
        build=lambda *_: None, suite_runner=lambda *_: None, llm_factory=(lambda *_: llm) if llm else None,
        pr_publisher=recorder,
    )  # fmt: skip
    payload = {
        "input": data.model_dump(mode="json"), "requirements_ref": req_ref.model_dump(),
        "source": {"workspace": str(base)},
        "results": [{"id": "pg", "status": "PASSED"}, {"id": "ing", "status": "FAILED"}],
        "row_changes": {}, "built_source_sha256": tree_sha256(base),
    }  # fmt: skip
    return activities, store, payload, recorder, base


@pytest.mark.asyncio
async def test_without_an_analysis_version_only_requirements_and_source_are_published(tmp_path):
    activities, _, payload, _, _ = world(tmp_path, with_analysis=False)
    planned = await activities.sbx_plan_publish(payload)
    assert planned["final_ref"] is None and planned["changed"] == []
    published = await activities.sbx_publish(payload)
    assert published["requirements_ref"] == payload["requirements_ref"]
    assert published["report_ref"] is None and published["patch"] is None


@pytest.mark.asyncio
async def test_the_published_report_has_the_version_the_plan_announced_and_verified_rows(tmp_path):
    activities, store, payload, _, _ = world(tmp_path)
    planned = await activities.sbx_plan_publish(payload)
    published = await activities.sbx_publish(payload)
    assert published["analysis_ref"] == planned["final_ref"] and planned["changed"] == []
    from cmp_worker.artifact_store import ArtifactRef

    report = AnalysisReport.model_validate_json(
        store.get(ArtifactRef.model_validate(published["report_ref"]))
    )
    status = {r.id: r.status for r in report.mapping}
    assert status["r1"] == "SANDBOX_VERIFIED" and status["r2"] == "EVIDENCE_CHECKED"
    assert report.ref.model_dump() == planned["final_ref"] and report.ref.version != REF.version
    assert published["review_ref"] is None  # nobody was asked again


@pytest.mark.asyncio
async def test_changed_rows_are_reported_and_their_new_answers_are_recorded_with_the_version(
    tmp_path,
):
    activities, store, payload, _, _ = world(tmp_path)
    payload["row_changes"] = {"r2": "KEEP"}
    planned = await activities.sbx_plan_publish(payload)
    assert [r["id"] for r in planned["changed"]] == ["r2"]
    assert planned["required"] == REQUIRED and planned["actor"] == "park"
    decisions = {"required": {"r2": False}, "approvals": {"r2": "APPROVED"}, "actor": "minseo"}
    published = await activities.sbx_publish({**payload, "decisions": decisions})
    from cmp_worker.artifact_store import ArtifactRef

    record = ReviewRecord.model_validate_json(
        store.get(ArtifactRef.model_validate(published["review_ref"]))
    )
    assert record.analysis_ref.model_dump() == planned["final_ref"] and record.actor == "minseo"
    assert record.approvals == {"r2": "APPROVED"}


@pytest.mark.asyncio
async def test_a_row_change_for_an_unknown_row_is_reported_not_raised(tmp_path):
    activities, _, payload, _, _ = world(tmp_path)
    payload["row_changes"] = {"nope": "KEEP"}
    planned = await activities.sbx_plan_publish(payload)
    assert planned["error"] == "UNKNOWN_ROW" and planned["final_ref"] is None


@pytest.mark.asyncio
async def test_source_that_changed_after_the_verification_is_never_published(tmp_path):
    activities, _, payload, _, base = world(tmp_path)
    (base / "main.go").write_text("package main\nvar port = 1\n")
    with pytest.raises(ApplicationError) as exc:
        await activities.sbx_publish(payload)
    assert exc.value.type == "SOURCE_CHANGED_AFTER_VERIFICATION" and exc.value.non_retryable


@pytest.mark.asyncio
async def test_a_code_fix_leaves_as_a_patch_that_reproduces_the_verified_source(tmp_path):
    activities, store, payload, recorder, base = world(tmp_path)
    fixed = tmp_path / "work" / "fix-1"
    fixed.mkdir(parents=True)
    (fixed / "main.go").write_text("package main\nvar port = 9898\n")
    payload = {**payload, "source": {"workspace": str(fixed)}, "built_source_sha256": tree_sha256(fixed),
               "fix_reasons": ["listen on 9898 key AKIAABCDEFGHIJKLMNOP"]}  # fmt: skip
    published = await activities.sbx_publish(payload)
    patch = published["patch"]
    assert patch["result_tree_sha256"] == tree_sha256(fixed) == published["source_sha256"]
    assert patch["publisher"] == {"status": "PREPARED", "branch": patch["branch"]}
    assert recorder.sent == [patch["branch"]]
    from cmp_worker.artifact_store import ArtifactRef
    from cmp_worker.fix_patch import apply_patch_tree_sha256

    text = store.get(ArtifactRef.model_validate(patch["patch_ref"]))
    assert apply_patch_tree_sha256(base, text) == tree_sha256(fixed)
    record = store.get_json(ArtifactRef.model_validate(patch["record_ref"]))
    assert "AKIAABCDEFGHIJKLMNOP" not in str(record)


@pytest.mark.asyncio
async def test_no_code_change_means_no_patch_and_nothing_is_offered(tmp_path):
    activities, _, payload, recorder, _ = world(tmp_path)
    published = await activities.sbx_publish(payload)
    assert published["patch"] is None and recorder.sent == []


def fix_llm(**kw):
    return FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_requirements_fix", reason="r", **kw)])]
    )


def fix_payload(payload, store, **extra):

    logs = store.put(b"log", ref_id="l", kind="sandbox-log")
    return {
        "classification": {"failure_class": "SPEC", "reasons": ["EGRESS_BLOCKED:x:443"]},
        "requirements_ref": payload["requirements_ref"], "logs_ref": logs.model_dump(),
        "suite_ref": payload["input"]["suite_ref"], "source": payload["source"],
        "analysis_report_ref": payload["input"]["analysis_report_ref"],
        "migration_id": payload["input"]["migration_id"], **extra,
    }  # fmt: skip


@pytest.mark.asyncio
async def test_a_fix_that_changes_a_known_row_returns_the_change(tmp_path):
    llm = fix_llm(row_changes=[{"row_id": "r2", "disposition": "KEEP"}])
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    result = await activities.sbx_fix(fix_payload(payload, store))
    assert result["status"] == "PROPOSED" and result["row_changes"] == {"r2": "KEEP"}


@pytest.mark.asyncio
async def test_a_fix_that_changes_an_unknown_row_is_not_accepted(tmp_path):
    llm = fix_llm(row_changes=[{"row_id": "ghost", "disposition": "KEEP"}])
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    result = await activities.sbx_fix(fix_payload(payload, store))
    assert result == {"status": "FAILED", "reason": "UNKNOWN_ROW"}


@pytest.mark.asyncio
async def test_a_fix_may_not_change_a_rows_grade_or_required_flag(tmp_path):
    bad = call(
        "submit_requirements_fix", reason="r",
        row_changes=[{"row_id": "r2", "disposition": "KEEP", "required": False}],
    )  # fmt: skip
    good = call(
        "submit_requirements_fix", reason="r",
        row_changes=[{"row_id": "r2", "disposition": "KEEP"}],
    )  # fmt: skip
    llm = FakeLLMClient([LLMResponse(tool_calls=[bad]), LLMResponse(tool_calls=[good])])
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    result = await activities.sbx_fix(fix_payload(payload, store))
    assert result["status"] == "PROPOSED" and result["row_changes"] == {"r2": "KEEP"}
    assert "extra_forbidden" in llm.calls[1].messages[-1].content  # the extra field was refused


def skills_dir(tmp_path):
    root = tmp_path / "skills"
    (root / "network").mkdir(parents=True)
    (root / "network" / "SKILL.md").write_text("NET-SKILL-BODY")
    (root / "fixer-triggers.yaml").write_text(
        "triggers:\n  - skill: network\n    reason_patterns: ['^EGRESS_BLOCKED:']\n"
    )
    return root


@pytest.mark.asyncio
async def test_the_fixer_is_offered_the_skill_that_fits_the_failure_and_can_read_it(tmp_path):
    keep = {"row_changes": [{"row_id": "r2", "disposition": "KEEP"}]}
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("load_skill", name="network")]),
            LLMResponse(tool_calls=[call("submit_requirements_fix", reason="r", **keep)]),
        ]
    )
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    activities.skills_root = skills_dir(tmp_path)
    result = await activities.sbx_fix(fix_payload(payload, store))  # reasons: EGRESS_BLOCKED:...
    assert result["status"] == "PROPOSED"
    assert "NET-SKILL-BODY" in llm.calls[1].messages[-1].content
    assert "NET-SKILL-BODY" not in llm.calls[0].messages[0].content  # asked for, not pushed


@pytest.mark.asyncio
async def test_a_failure_no_rule_fits_gets_no_skill_tool(tmp_path):
    llm = fix_llm(row_changes=[{"row_id": "r2", "disposition": "KEEP"}])
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    activities.skills_root = skills_dir(tmp_path)
    other = {"failure_class": "SPEC", "reasons": ["HEALTH_FAILED"]}
    result = await activities.sbx_fix(fix_payload(payload, store, classification=other))
    assert result["status"] == "PROPOSED"
    assert "load_skill" not in {tool.name for tool in llm.calls[0].tools}


def split_skills_dir(tmp_path):
    root = tmp_path / "skills"
    for name in ("storage-s3", "storage-gcs"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"{name.upper()}-BODY")
    (root / "fixer-triggers.yaml").write_text(
        "triggers:\n"
        "  - skill: storage-s3\n    reason_patterns: ['^EGRESS_BLOCKED:']\n"
        "  - skill: storage-gcs\n    targets: [GCP]\n    reason_patterns: ['^EGRESS_BLOCKED:']\n"
    )
    return root


async def offered_tools(tmp_path, target_provider):
    keep = {"row_changes": [{"row_id": "r2", "disposition": "KEEP"}]}
    llm = FakeLLMClient(
        [
            LLMResponse(tool_calls=[call("load_skill", name="storage-gcs")]),
            LLMResponse(tool_calls=[call("submit_requirements_fix", reason="r", **keep)]),
        ]
    )
    activities, store, payload, _, _ = world(tmp_path, llm=llm, target_provider=target_provider)
    activities.skills_root = split_skills_dir(tmp_path)
    result = await activities.sbx_fix(fix_payload(payload, store))
    assert result["status"] == "PROPOSED"
    return llm


@pytest.mark.asyncio
async def test_a_skill_for_the_target_is_offered_when_the_report_names_that_target(tmp_path):
    llm = await offered_tools(tmp_path, "GCP")
    sent = llm.calls[0].messages[0].content
    assert "storage-s3" in sent and "storage-gcs" in sent
    assert "STORAGE-GCS-BODY" in llm.calls[1].messages[-1].content  # the model asked for it


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ON_PREM", "AWS", None])
async def test_a_skill_for_another_target_or_an_unknown_one_is_not_offered(tmp_path, provider):
    llm = await offered_tools(tmp_path, provider)
    sent = llm.calls[0].messages[0].content
    assert "storage-s3" in sent and "storage-gcs" not in sent
    assert "STORAGE-GCS-BODY" not in llm.calls[1].messages[-1].content


class DictCache:
    """In-memory stand-in for the red-team cache: a plain get/put on a dict."""

    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def put(self, key, value):
        self.items[key] = value


SECURITY_FINDING = {
    "finding_id": "f-1", "category": "INFO_EXPOSURE", "severity": "LOW", "endpoint": "GET /stats",
    "summary": "s", "repro_summary": "r",
}  # fmt: skip


def security_fix_payload(payload, store, **extra):
    classification = {"failure_class": "SECURITY", "reasons": ["FINDING:f-1"]}
    return fix_payload(
        payload, store, classification=classification, findings=[SECURITY_FINDING], **extra
    )


def code_fix_llm():
    files = [{"path": "main.go", "content": "package main\nvar port = 8080\n// guarded\n"}]
    return FakeLLMClient(
        [LLMResponse(tool_calls=[call("submit_code_fix", reason="guard stats", files=files)])]
    )


@pytest.mark.asyncio
async def test_a_security_fix_is_stored_and_reused_for_the_same_source_and_findings(tmp_path):
    from types import SimpleNamespace

    cache = DictCache()
    llm = code_fix_llm()
    activities, store, payload, _, _ = world(tmp_path, llm=llm)
    activities.redteam = SimpleNamespace(cache=cache)
    first = await activities.sbx_fix(security_fix_payload(payload, store))
    assert first["status"] == "PROPOSED" and first["kind"] == "CODE" and len(llm.calls) == 1
    assert len(cache.items) == 1 and next(iter(cache.items)).startswith("redteam/fix/")

    again = FakeLLMClient([])  # a second call to the Fixer would fail
    activities.llm_factory = lambda *_: again
    second = await activities.sbx_fix(security_fix_payload(payload, store))
    assert again.calls == []
    assert second["status"] == "PROPOSED" and second["source"] == first["source"]


@pytest.mark.asyncio
async def test_the_fix_cache_is_not_read_when_the_run_turned_the_cache_off(tmp_path):
    from types import SimpleNamespace

    cache = DictCache()
    activities, store, payload, _, _ = world(tmp_path, llm=code_fix_llm())
    activities.redteam = SimpleNamespace(cache=cache)
    await activities.sbx_fix(security_fix_payload(payload, store))
    llm = code_fix_llm()
    activities.llm_factory = lambda *_: llm
    result = await activities.sbx_fix(security_fix_payload(payload, store, redteam_cache=False))
    assert result["status"] == "PROPOSED" and len(llm.calls) == 1  # the Fixer ran again


@pytest.mark.asyncio
async def test_other_failures_and_other_findings_do_not_use_the_fix_cache(tmp_path):
    from types import SimpleNamespace

    cache = DictCache()
    activities, store, payload, _, _ = world(tmp_path, llm=code_fix_llm())
    activities.redteam = SimpleNamespace(cache=cache)
    await activities.sbx_fix(security_fix_payload(payload, store))
    other = {**SECURITY_FINDING, "finding_id": "f-2"}
    llm = code_fix_llm()
    activities.llm_factory = lambda *_: llm
    classification = {"failure_class": "SECURITY", "reasons": ["FINDING:f-2"]}
    result = await activities.sbx_fix(
        fix_payload(payload, store, classification=classification, findings=[other])
    )
    assert result["status"] == "PROPOSED" and len(llm.calls) == 1  # different Findings: a miss
    assert len(cache.items) == 2
