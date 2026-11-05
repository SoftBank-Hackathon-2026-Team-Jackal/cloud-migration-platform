"""Real Sandbox loop Activities (ticket 14B).

Names start with `sbx_` so the scaffold Activities and every existing history stay untouched.
Payloads and results are small JSON: references, names, codes. Secrets, source text and logs
never enter Workflow history; logs are masked and stored by reference.
"""

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cmp_agents.fixer import FixInput, apply_code_fix, apply_requirements_fix, propose_fix
from cmp_agents.llm import LLMClient
from cmp_contracts.provisioning import Image
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_contracts.verification import canonical_requirements
from cmp_scanners.masking import mask_text
from cmp_testsuite import SuiteResult, load_bundle, run_suite
from pydantic import BaseModel, ConfigDict
from temporalio import activity

from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore
from cmp_worker.failure_classifier import (
    AttemptSignals,
    classify_failure,
    find_render_mismatches,
)
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
    keep_hosts: list[dict[str, Any]] = []
    source_hosts: list[str] = []


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
            )

    return run


class SandboxLoopActivities:
    def __init__(
        self,
        *,
        service: SandboxService,
        store: LocalArtifactStore,
        workspace_root: Path | str,
        build: Builder,
        suite_runner: SuiteRunner,
        llm_factory: Callable[[], LLMClient] | None = None,
    ) -> None:
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
        images = {
            r.id: self.build(r.id, payload["source"]).model_dump(mode="json")
            for r in requirements.resources
            if r.kind == "APPLICATION"
        }
        return {"images": images}

    @activity.defn(name="sbx_prepare")
    async def sbx_prepare(self, payload: dict) -> dict:
        inp = SandboxInput.model_validate(payload["input"])
        requirements = self._requirements(payload["requirements_ref"])
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
            "results": [{"id": r.id, "status": r.status} for r in suite.results],
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
            llm=self.llm_factory(),
            workspace=workspace,
        )
        if outcome.status != "PROPOSED":
            return {"status": outcome.status, "reason": outcome.reason}
        if outcome.requirements_fix is not None:
            fixed = apply_requirements_fix(requirements, outcome.requirements_fix)
            data, _ = canonical_requirements(fixed)
            ref = self.store.put(data, ref_id="requirements", kind="requirements")
            return {
                "status": "PROPOSED", "kind": "REQUIREMENTS", "reason": outcome.requirements_fix.reason,
                "requirements_ref": ref.model_dump(mode="json"),
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

    @activity.defn(name="sbx_teardown")
    async def sbx_teardown(self, payload: dict) -> dict:
        self.service.teardown_sandbox(payload["namespace"])
        return {"cleanup_status": "CLEANED"}

    def all(self) -> list[Callable]:
        return [
            self.sbx_build, self.sbx_prepare, self.sbx_apply, self.sbx_verify,
            self.sbx_classify, self.sbx_fix, self.sbx_teardown,
        ]  # fmt: skip
