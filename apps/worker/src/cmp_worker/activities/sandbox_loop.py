"""Real Sandbox loop Activities (ticket 14B).

Names start with `sbx_` so the scaffold Activities and every existing history stay untouched.
Payloads and results are small JSON: references, names, codes. Secrets, source text and logs
never enter Workflow history; logs are masked and stored by reference.
"""

import json
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cmp_agents.fixer import FixInput, apply_code_fix, apply_requirements_fix, propose_fix
from cmp_agents.llm import LLMClient
from cmp_agents.tester import SuiteRequest, generate_tests
from cmp_contracts.analysis import AnalysisReport, EnvExceptions, ReviewRecord
from cmp_contracts.env_plan import EnvPlan
from cmp_contracts.provisioning import ContentRef, Image, VersionRef
from cmp_contracts.provisioning_v2 import RequirementsV2, S3ArchiveRef
from cmp_contracts.verification import BuildAttempt, canonical_requirements
from cmp_providers.ecr_pull import EcrTokenMinter, PlatformRegistry
from cmp_providers.env_file import EnvFileStore
from cmp_scanners.masking import mask_text
from cmp_scanners.routes import scan_go_routes
from cmp_testsuite import SuiteResult, load_bundle, run_suite
from cmp_testsuite.model import HttpStep
from cmp_testsuite.record import RECORDED_PREFIX, Fetch, record_probe
from cmp_testsuite.runner import run_case
from cmp_testsuite.validate import CaseRunner, pin_suite, validate_tests
from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore
from cmp_worker.build_wiring import BuildFailed, to_build_attempt
from cmp_worker.failure_classifier import (
    AttemptSignals,
    classify_failure,
    find_render_mismatches,
)
from cmp_worker.final_report import (
    FinalReportError,
    apply_row_changes,
    build_final_report,
    changed_rows,
    mark_verified,
    with_decisions,
)
from cmp_worker.fix_patch import (
    DraftPrPublisher,
    PatchError,
    PreparedOnlyPublisher,
    build_patch_artifact,
    tree_sha256,
)
from cmp_worker.planning_handoff import (
    HandoffError,
    PlanningSettings,
    assemble_registration,
    check_with_planning,
)
from cmp_worker.prebuilt_builder import BuildUnavailable
from cmp_worker.sandbox_deploy import (
    SandboxCapabilities,
    SandboxError,
    SandboxImages,
    SandboxPlan,
    SandboxService,
)
from cmp_worker.sandbox_judge import judge

SuiteRunner = Callable[[SandboxPlan, ArtifactRef, LocalArtifactStore], SuiteResult]
Builder = Callable[[str, dict[str, Any]], Image]

_TRACE = re.compile(r"panic:|Traceback \(most recent call last\)|goroutine \d+ \[|Exception in")
_FILE_REF = re.compile(r"([\w./@-]+\.(?:go|py|js|ts|java|rb))(?::\d+|\", line \d+)")
_APP_CAUSE_EVENTS = {"Unhealthy", "BackOff", "CrashLoopBackOff", "Failed", "FailedScheduling"}


class SandboxInput(BaseModel):
    """Workflow argument. References and names only."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    run_id: str
    requirements_ref: ArtifactRef
    suite_ref: ArtifactRef
    source: dict[str, Any]  # {"workspace": <path>} until the S3 source archive is wired
    database_image: Image
    storage_class: str = "local-path"
    env_names: list[str] = []
    env_file: str | None = None
    env_file_ref: ContentRef | None = None  # ENV-02: the uploaded `.env` (reference only)
    env_plan_ref: ArtifactRef | None = None  # ENV-05: the per-key plan the analysis stored
    keep_hosts: list[dict[str, Any]] = []
    source_hosts: list[str] = []
    ai_tests: bool = (
        False  # ask the Tester once the first Sandbox is healthy (needs an origin runner)
    )
    analysis_report_ref: ArtifactRef | None = None  # the analysis version a person approved
    review_ref: ArtifactRef | None = None  # its stored ReviewRecord


def stack_in_app_code(logs: str, workspace: Path | str | None) -> bool:
    """True when a panic or traceback in the logs points at a file of the application."""
    if not workspace or not _TRACE.search(logs):
        return False
    root = Path(workspace)
    names = {p.name for p in list(root.rglob("*"))[:5000] if p.is_file()}
    return any(Path(m.group(1)).name in names for m in _FILE_REF.finditer(logs))


def make_proxy_suite_runner(service: SandboxService) -> SuiteRunner:
    """Run the pinned suite from outside the cluster through `kubectl proxy`."""

    def run(plan: SandboxPlan, suite_ref: ArtifactRef, store: LocalArtifactStore) -> SuiteResult:
        app = next(r for r in plan.request["resources"] if r["kind"] == "APPLICATION")
        with service.cluster.proxy() as proxy:
            return run_suite(
                suite_ref,
                proxy + service.service_path(plan, app["id"]),
                store=store,
                actions={"RESTART_DATABASE": lambda: service.restart_database(plan)},
                variables={"health_path": app["health_path"]},
            )

    return run


def make_proxy_case_runner(
    service: SandboxService,
) -> Callable[[SandboxPlan], AbstractContextManager[CaseRunner]]:
    """Run one case at a time against the Sandbox application through `kubectl proxy`."""

    @contextmanager
    def open_runner(plan: SandboxPlan) -> Iterator[CaseRunner]:
        app = next(r for r in plan.request["resources"] if r["kind"] == "APPLICATION")
        with service.cluster.proxy() as proxy:
            base = proxy + service.service_path(plan, app["id"])
            variables = {"health_path": app["health_path"], "run_id": "ai-check"}
            yield lambda case: run_case(case, base, variables=dict(variables))

    return open_runner


def make_proxy_fetcher(
    service: SandboxService,
) -> Callable[[SandboxPlan], AbstractContextManager[Fetch]]:
    """GET one path of the Sandbox application through `kubectl proxy` (for recording answers)."""

    @contextmanager
    def open_fetcher(plan: SandboxPlan) -> Iterator[Fetch]:
        app = next(r for r in plan.request["resources"] if r["kind"] == "APPLICATION")
        with service.cluster.proxy() as proxy:
            base = proxy + service.service_path(plan, app["id"])

            def fetch(path: str) -> tuple[int, bytes]:
                try:
                    with urllib.request.urlopen(base + path, timeout=15) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            yield fetch

    return open_fetcher


class SandboxLoopActivities:
    def __init__(
        self,
        *,
        service: SandboxService,
        store: LocalArtifactStore,
        workspace_root: Path | str,
        build: Builder,
        suite_runner: SuiteRunner,
        llm_factory: Callable[[str, str], LLMClient] | None = None,
        pr_publisher: DraftPrPublisher | None = None,
        target_case_runner: Callable[[SandboxPlan], AbstractContextManager[CaseRunner]]
        | None = None,
        origin_case_runner: Callable[[], AbstractContextManager[CaseRunner]] | None = None,
        cut_dependency: Callable[[SandboxPlan], AbstractContextManager[None]] | None = None,
        suite_uploader: Callable[[bytes], S3ArchiveRef] | None = None,
        env_store: EnvFileStore | None = None,
        pull_registry: PlatformRegistry | None = None,
        token_minter: EcrTokenMinter | None = None,
        target_fetcher: Callable[[SandboxPlan], AbstractContextManager[Fetch]] | None = None,
        reset_data: Callable[[SandboxPlan], Any] | None = None,
        skills_root: Path | str | None = None,
    ) -> None:
        self.target_fetcher = target_fetcher or make_proxy_fetcher(service)  # ticket 27-B
        self.reset_data = reset_data  # default: the service's own, looked up when needed
        self.skills_root = Path(skills_root) if skills_root else None  # where `test-authoring` is
        self.pull_registry = pull_registry  # ticket 26: private platform ECR images
        self.token_minter = token_minter
        self.env_store = env_store  # ENV-06: key names and PASS values by reference
        self.suite_uploader = suite_uploader
        # The origin test resource (D-09) has no default. Without it the Tester is still asked, but
        # only for shapes and probes: tests whose expected values need no outside answer (27-B).
        self.origin_case_runner = origin_case_runner
        self.target_case_runner = target_case_runner or make_proxy_case_runner(service)
        self.cut_dependency = cut_dependency or service.database_cut
        self.pr_publisher = pr_publisher or PreparedOnlyPublisher()
        self.service = service
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.build = build
        self.suite_runner = suite_runner
        self.llm_factory = llm_factory

    # ---------------------------------------------------------------- helpers

    def _requirements(self, ref: dict) -> RequirementsV2:
        return RequirementsV2.model_validate_json(self.store.get(ArtifactRef.model_validate(ref)))

    # ------------------------------------------------------------- activities

    @activity.defn(name="sbx_build")
    async def sbx_build(self, payload: dict) -> dict:
        """Build every application image from the current source."""
        requirements = self._requirements(payload["requirements_ref"])
        with_evidence = getattr(self.build, "build_with_evidence", None)
        if with_evidence is not None:
            return self._build_with_evidence(payload, requirements, with_evidence)
        try:
            images = {
                r.id: self.build(r.id, payload["source"]).model_dump(mode="json")
                for r in requirements.resources
                if r.kind == "APPLICATION"
            }
        except BuildUnavailable as exc:
            raise ApplicationError(
                "No application image could be provided", type=exc.code, non_retryable=True
            ) from None
        workspace = payload["source"].get("workspace")
        source_sha256 = tree_sha256(workspace) if workspace and Path(workspace).is_dir() else None
        return {"images": images, "source_sha256": source_sha256}

    def _build_with_evidence(self, payload: dict, requirements: RequirementsV2, build) -> dict:
        """The real build path: images plus the `BuildAttempt` records Planning needs (ticket 25)."""
        migration_id, run_id = payload.get("migration_id"), payload.get("run_id")
        if not migration_id or not run_id:
            raise ApplicationError(
                "Build context is missing", type="BUILD_CONTEXT_MISSING", non_retryable=True
            )
        images: dict[str, Any] = {}
        attempts = []
        try:
            for resource in requirements.resources:
                if resource.kind != "APPLICATION":
                    continue
                image, result = build(
                    resource.id,
                    payload["source"],
                    migration_id=migration_id,
                    run_id=run_id,
                    attempt=payload.get("attempt", 1),
                )
                images[resource.id] = image.model_dump(mode="json")
                attempts.append(to_build_attempt(result, migration_id=migration_id))
        except BuildFailed as exc:
            return {"images": None, "build_error": exc.code, "failure_class": exc.failure_class}
        data = json.dumps(
            [a.model_dump(mode="json") for a in attempts], sort_keys=True, separators=(",", ":")
        )
        builds_ref = self.store.put(data.encode(), ref_id="build-attempts", kind="build-attempts")
        workspace = payload["source"].get("workspace")
        source_sha256 = tree_sha256(workspace) if workspace and Path(workspace).is_dir() else None
        return {
            "images": images,
            "source_sha256": source_sha256,
            "builds_ref": builds_ref.model_dump(mode="json"),
        }

    @activity.defn(name="sbx_prepare")
    async def sbx_prepare(self, payload: dict) -> dict:
        inp = SandboxInput.model_validate(payload["input"])
        requirements = self._requirements(payload["requirements_ref"])
        env_plan = (
            EnvPlan.model_validate_json(self.store.get(inp.env_plan_ref))
            if inp.env_plan_ref is not None
            else None
        )
        images = SandboxImages(
            database=inp.database_image,
            applications={k: Image.model_validate(v) for k, v in payload["images"].items()},
        )
        try:
            plan = self.service.prepare_sandbox(
                requirements,
                migration_id=inp.migration_id,
                run_id=inp.run_id,
                images=images,
                capabilities=SandboxCapabilities(storage_class=inp.storage_class),
                env_names=inp.env_names,
                env_file=Path(inp.env_file) if inp.env_file else None,
                env_plan=env_plan,
                env_file_ref=inp.env_file_ref,
                env_store=self.env_store,
                update_env=payload.get("previous_plan") is not None,
                network=True,
            )
        except SandboxError as exc:
            return {"plan": None, "sandbox_error": exc.code}
        return {"plan": plan.model_dump(mode="json"), "sandbox_error": None}

    @activity.defn(name="sbx_apply")
    async def sbx_apply(self, payload: dict) -> dict:
        """First attempt installs; later attempts change only what differs."""
        inp = SandboxInput.model_validate(payload["input"])
        plan = SandboxPlan.model_validate(payload["plan"])
        previous = payload.get("previous_plan")
        result: dict[str, Any] = {"sandbox_error": None, "baseline": None, "actions": {}}
        try:
            self.service.apply_pull_secret(plan, self.pull_registry, self.token_minter)
            if previous is None:
                self.service.apply_sandbox(plan)
                self.service.apply_network(plan, inp.keep_hosts)
                self.service.check_sandbox(plan)
                self.service.snapshot_baseline(plan)
                result["baseline"] = "SAVED"
            else:
                record = self.service.update_sandbox(
                    SandboxPlan.model_validate(previous), plan, payload["attempt"]
                )
                self.service.apply_network(plan, inp.keep_hosts)
                self.service.check_sandbox(plan)
                # A failed first attempt may have left no baseline behind; take one now.
                need = record.baseline == "LOST" or not payload.get("baseline_ok")
                if need:
                    self.service.snapshot_baseline(plan)
                result["baseline"] = "SAVED" if need else "KEPT"
                result["record"] = record.model_dump(mode="json")
        except SandboxError as exc:
            result["sandbox_error"] = exc.code
        return result

    @activity.defn(name="sbx_verify")
    async def sbx_verify(self, payload: dict) -> dict:
        """Run the pinned suite and judge; collect the signals a classification needs."""
        inp = SandboxInput.model_validate(payload["input"])
        plan = SandboxPlan.model_validate(payload["plan"])
        applied = payload["applied"]
        code = applied.get("sandbox_error")
        events = self.service.pod_event_reasons(plan)
        logs = mask_text(self.service.app_logs(plan))
        logs_ref = self.store.put(logs.encode(), ref_id="sandbox-logs", kind="sandbox-log")
        base: dict[str, Any] = {
            "sandbox_error": code,
            "pod_events": events,
            "app_cause_in_pod": bool(set(events) & _APP_CAUSE_EVENTS),
            "stack_in_app_code": stack_in_app_code(logs, payload["source"].get("workspace")),
            "logs_ref": logs_ref.model_dump(mode="json"),
        }
        if code is not None:
            reasons = [f"SANDBOX:{code}"]
            return {**base, "verdict": {"passed": False, "reasons": reasons}, "results": []}
        try:
            if payload.get("reset"):
                self.service.reset_test_data(plan)
            suite_ref = ArtifactRef.model_validate(payload["suite_ref"])
            suite = self.suite_runner(plan, suite_ref, self.store)
            egress = self.service.egress_records(plan)
        except SandboxError as exc:
            return {
                **base, "sandbox_error": exc.code, "results": [],
                "verdict": {"passed": False, "reasons": [f"SANDBOX:{exc.code}"]},
            }  # fmt: skip
        verdict = judge(results=suite, egress=egress, source_hosts=set(inp.source_hosts))
        return {
            **base,
            "verdict": verdict.model_dump(mode="json"),
            "results": [
                {"id": r.id, "status": r.status, **({"reason": r.reason} if r.reason else {})}
                for r in suite.results
            ],  # a reason is a code (status numbers, field names), never response content
        }

    @activity.defn(name="sbx_classify")
    async def sbx_classify(self, payload: dict) -> dict:
        from cmp_contracts.verification import Verdict

        plan = SandboxPlan.model_validate(payload["plan"])
        requirements = self._requirements(payload["requirements_ref"])
        verified = payload["verified"]
        signals = AttemptSignals(
            sandbox_error=verified.get("sandbox_error"),
            pod_events=verified.get("pod_events", []),
            infra_timeout=verified.get("sandbox_error") == "CLUSTER_COMMAND_FAILED",
            app_cause_in_pod=verified.get("app_cause_in_pod", False),
            render_mismatches=find_render_mismatches(requirements, plan.request),
            build_failure_class=payload.get("build_failure_class"),
            verdict=Verdict.model_validate(verified["verdict"]),
            stack_in_app_code=verified.get("stack_in_app_code", False),
        )
        result = classify_failure(signals)
        return {
            "failure_class": result.failure_class,
            "reasons": result.reasons,
            "fixer_allowed": result.fixer_allowed,
        }

    @activity.defn(name="sbx_fix")
    async def sbx_fix(self, payload: dict) -> dict:
        if self.llm_factory is None:
            return {"status": "FAILED", "reason": "LLM_NOT_CONFIGURED"}
        klass = payload["classification"]["failure_class"]
        requirements = self._requirements(payload["requirements_ref"])
        logs = self.store.get(ArtifactRef.model_validate(payload["logs_ref"])).decode()
        suite = load_bundle(self.store.get(ArtifactRef.model_validate(payload["suite_ref"])))
        workspace = payload["source"].get("workspace")
        outcome = propose_fix(
            FixInput(
                failure_class=klass,
                reasons=payload["classification"]["reasons"],
                requirements_json=canonical_requirements(requirements)[0].decode(),
                logs=logs,
                tests={c.id: c.model_dump(mode="json") for c in suite.cases},
            ),
            llm=self.llm_factory(payload["migration_id"], "fixer"),
            workspace=workspace,
        )
        if outcome.status != "PROPOSED":
            return {"status": outcome.status, "reason": outcome.reason}
        if outcome.requirements_fix is not None:
            changes = {c.row_id: c.disposition for c in outcome.requirements_fix.row_changes}
            if changes:
                try:
                    apply_row_changes(
                        self._report_rows(payload.get("analysis_report_ref")), changes
                    )
                except FinalReportError as exc:
                    return {"status": "FAILED", "reason": exc.code}
            fixed = apply_requirements_fix(requirements, outcome.requirements_fix)
            data, _ = canonical_requirements(fixed)
            ref = self.store.put(data, ref_id="requirements", kind="requirements")
            return {
                "status": "PROPOSED", "kind": "REQUIREMENTS", "reason": outcome.requirements_fix.reason,
                "requirements_ref": ref.model_dump(mode="json"), "row_changes": changes,
            }  # fmt: skip
        fix = outcome.code_fix
        digest = self.store.put(
            "\n".join(f"{c.path}\0{c.content}" for c in fix.files).encode(),
            ref_id="code-fix", kind="code-fix",
        ).sha256  # fmt: skip
        target = self.workspace_root / f"fix-{digest[:16]}"
        if not target.exists():
            shutil.copytree(workspace, target, ignore=shutil.ignore_patterns(".git"))
        changed = apply_code_fix(target, fix)
        return {
            "status": "PROPOSED", "kind": "CODE", "reason": fix.reason, "changed": changed,
            "source": {"workspace": str(target)},
        }  # fmt: skip

    @activity.defn(name="sbx_ai_tests")
    async def sbx_ai_tests(self, payload: dict) -> dict:
        """Ask the Tester for tests and keep only those that pass the four conditions (AV-012).

        The Tester reads the source and proposes write shapes and read-only probes with citations;
        it does not write expected values. Shapes become property tests, probes are asked of the
        app twice and kept as recorded tests, and a free-form test is only taken when an origin
        test resource can vouch for it (ticket 27-B). Without a model nothing is asked.
        """
        suite_ref = ArtifactRef.model_validate(payload["suite_ref"])
        same = {"suite_ref": suite_ref.model_dump(mode="json"), "accepted": [], "rejected": {}}
        if self.llm_factory is None:
            return {**same, "status": "NOT_CALLED", "reason": "LLM_NOT_CONFIGURED"}
        suite = load_bundle(self.store.get(suite_ref))
        routes = {
            f"{step.method} {step.path}": "used by a standard test"
            for c in suite.cases
            for step in c.steps
            if isinstance(step, HttpStep)
        }
        workspace = (payload.get("input", {}).get("source") or {}).get("workspace")
        source = Path(workspace) if workspace and Path(workspace).is_dir() else None
        report = self._report(payload.get("analysis_report_ref"))
        usages = report.usage_profile.usages if report else []
        generated = generate_tests(
            SuiteRequest(
                usage_summary="\n".join(usages) or "(none)",
                api_routes=routes,
                existing_ids=[c.id for c in suite.cases],
                http_routes=scan_go_routes(source) if source else [],
                workspace=source,
                skills_root=self.skills_root,
                origin_available=self.origin_case_runner is not None,
            ),
            llm=self.llm_factory(payload["migration_id"], "tester"),
        )
        if generated.status != "GENERATED":
            return {**same, "status": "FAILED", "reason": generated.failure_reason}
        plan = SandboxPlan.model_validate(payload["plan"])
        rejected: dict[str, str] = {}
        recorded = []
        if generated.probes:
            # Recorded first, on the data the Sandbox starts with: later cases write.
            with self.target_fetcher(plan) as fetch:
                for probe in generated.probes:
                    result = record_probe(probe, fetch, sensitive=lambda t: mask_text(t) != t)
                    if result.case is None:
                        rejected[f"{RECORDED_PREFIX}{probe.id}"] = result.reason or "RECORD_FAILED"
                    else:
                        recorded.append(result.case)
        origin = self.origin_case_runner() if self.origin_case_runner else nullcontext(None)
        with origin as origin_runner, self.target_case_runner(plan) as target:
            validation = validate_tests(
                [*recorded, *generated.cases],
                target=target,
                origin=origin_runner,
                cut_dependency=lambda: self.cut_dependency(plan),
                workspace=source,
            )
        accepted = list(validation.accepted)
        rejected.update({v.case_id: v.reason for v in validation.verdicts if not v.accepted})
        if any(c.basis == "RECORDED" for c in accepted):
            try:  # the checks above wrote data; the recorded answers belong to the baseline
                (self.reset_data or self.service.reset_test_data)(plan)
            except SandboxError:
                rejected.update({c.id: "RESET_FAILED" for c in accepted if c.basis == "RECORDED"})
                accepted = [c for c in accepted if c.basis != "RECORDED"]
        if not accepted:
            return {**same, "status": "NONE_ACCEPTED", "rejected": rejected}
        _pinned, data = pin_suite(f"{suite.name}+ai", suite.cases, accepted)
        ref = self.store.put(data, ref_id="suite-ai", kind="test-suite")
        return {
            "status": "PINNED", "suite_ref": ref.model_dump(mode="json"),
            "accepted": [c.id for c in accepted], "rejected": rejected,
        }  # fmt: skip

    # ------------------------------------------------------------------ publication

    def _report(self, ref: dict | None) -> AnalysisReport | None:
        if ref is None:
            return None
        return AnalysisReport.model_validate_json(self.store.get(ArtifactRef.model_validate(ref)))

    def _report_rows(self, ref: dict | None) -> list:
        report = self._report(ref)
        return list(report.mapping) if report else []

    def _final(self, payload: dict) -> dict[str, Any]:
        """The final analysis version, rows and the first review's decisions (pure)."""
        inp = SandboxInput.model_validate(payload["input"])
        requirements_ref = ArtifactRef.model_validate(payload["requirements_ref"])
        report = self._report(
            inp.analysis_report_ref.model_dump() if inp.analysis_report_ref else None
        )
        if report is None:
            return {"report": None, "changed": [], "required": {}, "approvals": {}, "actor": ""}
        required, approvals, actor = {}, {}, ""
        if inp.review_ref is not None:
            record = ReviewRecord.model_validate_json(self.store.get(inp.review_ref))
            required, approvals, actor = dict(record.required), dict(record.approvals), record.actor
        before = with_decisions(report.mapping, required)
        after = with_decisions(
            apply_row_changes(before, payload.get("row_changes") or {}), required
        )
        suite = load_bundle(self.store.get(ArtifactRef.model_validate(inp.suite_ref)))
        verified = mark_verified(after, suite, payload.get("results") or [])
        final = build_final_report(report, verified, requirements_ref.sha256)
        return {
            "report": final, "changed": changed_rows(before, after), "required": required,
            "approvals": approvals, "actor": actor,
        }  # fmt: skip

    @activity.defn(name="sbx_plan_publish")
    async def sbx_plan_publish(self, payload: dict) -> dict:
        """Which rows changed handling since a person approved them (they need a new answer)."""
        try:
            final = self._final(payload)
        except FinalReportError as exc:
            return {
                "error": exc.code,
                "changed": [],
                "final_ref": None,
                "required": {},
                "actor": "",
            }
        report = final["report"]
        return {
            "error": None,
            "final_ref": report.ref.model_dump() if report else None,
            "changed": [r.model_dump(mode="json") for r in final["changed"]],
            "required": final["required"],
            "actor": final["actor"],
        }

    @activity.defn(name="sbx_publish")
    async def sbx_publish(self, payload: dict) -> dict:
        """Publish exactly what the last verification used; a patch for any code change."""
        inp = SandboxInput.model_validate(payload["input"])
        source = payload["source"]
        workspace = source.get("workspace")
        built = payload.get("built_source_sha256")
        source_sha256 = tree_sha256(workspace) if workspace and Path(workspace).is_dir() else None
        if built is not None and source_sha256 != built:
            raise ApplicationError(
                "Source changed after it was verified",
                type="SOURCE_CHANGED_AFTER_VERIFICATION", non_retryable=True,
            )  # fmt: skip
        result: dict[str, Any] = {
            "status": "PUBLISHED",
            "requirements_ref": payload["requirements_ref"],
            "source": source,
            "source_sha256": source_sha256,
            "report_ref": None,
            "review_ref": None,
            "patch": None,
        }
        final = self._final(payload)
        if final["report"] is not None:
            report = final["report"]
            ref = self.store.put_json(
                report, ref_id=f"analysis-{inp.migration_id}", kind="analysis-report"
            )
            result["report_ref"] = ref.model_dump(mode="json")
            result["analysis_ref"] = report.ref.model_dump(mode="json")
            decisions = payload.get("decisions")
            if decisions:
                record = ReviewRecord.model_validate(
                    {
                        "analysis_ref": report.ref.model_dump(),
                        "required": decisions["required"],
                        "operational": decisions.get("operational", []),
                        "approvals": {**final["approvals"], **decisions["approvals"]},
                        "actor": decisions["actor"],
                        "decided_at": datetime.now(UTC),
                    }
                )
                review = self.store.put_json(
                    record, ref_id=f"review-{inp.migration_id}", kind="review-record"
                )
                result["review_ref"] = review.model_dump(mode="json")
        base = inp.source.get("workspace")
        if workspace and base and workspace != base:
            try:
                artifact = build_patch_artifact(
                    base, workspace, migration_id=inp.migration_id,
                    reasons=payload.get("fix_reasons") or [],
                )  # fmt: skip
            except PatchError as exc:
                raise ApplicationError(
                    "Patch does not reproduce the verified source",
                    type=exc.code,
                    non_retryable=True,
                ) from None
            if artifact is not None:
                patch_ref = self.store.put(artifact.patch, ref_id="fix-patch", kind="fix-patch")
                record_ref = self.store.put_json(
                    {
                        "branch": artifact.branch, "title": artifact.title, "body": artifact.body,
                        "base_tree_sha256": artifact.base_tree_sha256,
                        "result_tree_sha256": artifact.result_tree_sha256,
                        "patch_ref": patch_ref.model_dump(mode="json"),
                    },
                    ref_id="fix-patch-record", kind="fix-patch-record",
                )  # fmt: skip
                result["patch"] = {
                    "record_ref": record_ref.model_dump(mode="json"),
                    "patch_ref": patch_ref.model_dump(mode="json"),
                    "branch": artifact.branch,
                    "result_tree_sha256": artifact.result_tree_sha256,
                    "publisher": self.pr_publisher.publish(
                        artifact, patch_ref.model_dump(mode="json")
                    ),
                }
        return result

    @activity.defn(name="sbx_plan_assemble")
    async def sbx_plan_assemble(self, payload: dict) -> dict:
        """Build the Planning input from the passed run's recorded evidence (ticket 15)."""
        try:
            return {"status": "READY", **self._assemble(payload)}
        except HandoffError as exc:
            return {"status": "REFUSED", "code": exc.code}

    def _assemble(self, payload: dict) -> dict:
        settings = PlanningSettings.model_validate(payload["settings"])
        sandbox, analysis = payload["sandbox"], payload["analysis"]
        published = sandbox.get("published") or {}
        if sandbox.get("status") != "PASSED" or published.get("status") != "PUBLISHED":
            raise HandoffError("SANDBOX_NOT_PASSED")
        if not sandbox.get("builds_ref"):
            raise HandoffError("BUILD_EVIDENCE_MISSING")
        if self.suite_uploader is None:
            raise HandoffError("TEST_SUITE_UPLOAD_NOT_CONFIGURED")

        def content(ref: dict) -> ContentRef:
            return ContentRef(id=ref["id"], version=ref["version"], sha256=ref["sha256"])

        report_ref = ArtifactRef.model_validate(published["report_ref"])
        report = AnalysisReport.model_validate_json(self.store.get(report_ref))
        requirements_ref = ArtifactRef.model_validate(published["requirements_ref"])
        requirements_bytes = self.store.get(requirements_ref)
        builds = [
            BuildAttempt.model_validate(item)
            for item in json.loads(
                self.store.get(ArtifactRef.model_validate(sandbox["builds_ref"]))
            )
        ]
        suite_bundle = self.store.get(ArtifactRef.model_validate(payload["suite_ref"]))
        assembly = json.loads(self.store.get(ArtifactRef.model_validate(analysis["assembly_ref"])))
        exceptions = EnvExceptions.model_validate(assembly["env_exceptions"])
        plan_ref = assembly.get("env_plan_ref")  # ENV-05: the approved per-key plan, names only
        env_plan = (
            EnvPlan.model_validate_json(self.store.get(ArtifactRef.model_validate(plan_ref)))
            if plan_ref
            else None
        )
        mapping = self.store.put_json(
            exceptions, ref_id=f"env-exceptions-{report.migration_id}", kind="env-exceptions"
        )
        registration = assemble_registration(
            report=report,
            requirements_ref=content(requirements_ref.model_dump()),
            report_ref=content(report_ref.model_dump()),
            builds=builds,
            attempts=sandbox["attempts"],
            suite_ref=self.suite_uploader(suite_bundle),
            mapping_ref=content(mapping.model_dump()),
            settings=settings,
            env_plan=env_plan,
            expected_target_ref=(
                VersionRef.model_validate(payload["target_selection_ref"])
                if payload.get("target_selection_ref")
                else None
            ),
        )
        check_with_planning(registration, requirements_bytes)
        stored = self.store.put_json(
            registration, ref_id=f"planning-input-{report.migration_id}", kind="planning-input"
        )
        return {"registration_ref": stored.model_dump(mode="json")}

    @activity.defn(name="sbx_teardown")
    async def sbx_teardown(self, payload: dict) -> dict:
        self.service.teardown_sandbox(payload["namespace"])
        return {"cleanup_status": "CLEANED"}

    def all(self) -> list[Callable]:
        return [
            self.sbx_build, self.sbx_prepare, self.sbx_apply, self.sbx_verify,
            self.sbx_classify, self.sbx_fix, self.sbx_ai_tests, self.sbx_plan_assemble, self.sbx_plan_publish, self.sbx_publish,
            self.sbx_teardown,
        ]  # fmt: skip
