"""Real Sandbox on the dedicated `cmp-sandbox` k3d cluster. Opt in with:

  CMP_SANDBOX_KUBECONFIG=~/.kube/cmp-sandbox.yaml uv run pytest tests/integration/sandbox -m k3d

Only the `k3d-cmp-sandbox` context is ever used; any other context is refused.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from cmp_contracts.provisioning import Image
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_worker.activities.k8s_workloads_cli import CommandRunner
from cmp_worker.sandbox_deploy import (
    SANDBOX_CONTEXT,
    SandboxCapabilities,
    SandboxCluster,
    SandboxImages,
    SandboxService,
)

ROOT = Path(__file__).parent.parent.parent.parent
KUBECONFIG = os.environ.get("CMP_SANDBOX_KUBECONFIG")
pytestmark = [
    pytest.mark.k3d,
    pytest.mark.skipif(not KUBECONFIG, reason="set CMP_SANDBOX_KUBECONFIG to run the k3d tests"),
]

# Public images pinned by digest (resolved from the registries on 2026-11-05).
POSTGRES = Image(
    uri="docker.io/library/postgres@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a",
    platforms=["linux/amd64", "linux/arm64"],
)
PODINFO = Image(
    uri="ghcr.io/stefanprodan/podinfo@sha256:862ca45e61b32392f7941a1bdfdbe5ff8b6899070135f1bdca1c287d0057fc94",
    platforms=["linux/amd64", "linux/arm64"],
)
PODINFO_OLD = Image(
    uri="ghcr.io/stefanprodan/podinfo@sha256:d2b3cd93a48acdc91327533ce28fcb3169b2d9feaf73817dc2eb68858df64edb",
    platforms=["linux/amd64", "linux/arm64"],
)
ENV_VALUE = "k3d-env-secret-value-7788"


def requirements():
    return RequirementsV2.model_validate(
        {
            "contract_version": "2",
            "resources": [
                {
                    "id": "demo-api", "kind": "APPLICATION", "replicas": 1, "cpu_millicores": 100,
                    "memory_mib": 128, "port": 9898, "health_path": "/healthz",
                    "env_bindings": {
                        "DB_HOST": {"resource_id": "demo-db", "output": "host"},
                        "DB_PASSWORD": {"resource_id": "demo-db", "output": "password_secret_ref"},
                        "DATABASE_URL": {"resource_id": "demo-db", "output": "connection_url_secret_ref"},
                    },
                },
                {
                    "id": "demo-db", "kind": "POSTGRESQL", "version": "16", "extensions": [],
                    "database": {"name": "demoapp", "username": "demoapp"},
                    "cpu_millicores": 250, "memory_mib": 512, "storage_gib": 1,
                },
            ],
        }
    )  # fmt: skip


def kubectl(*args):
    return subprocess.run(
        ["kubectl", "--kubeconfig", str(Path(KUBECONFIG).expanduser()), "--context", SANDBOX_CONTEXT, *args],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


@pytest.fixture
def service():
    cluster = SandboxCluster(
        CommandRunner(), kubeconfig=Path(KUBECONFIG).expanduser(), context=SANDBOX_CONTEXT
    )
    return SandboxService(
        cluster,
        database_chart_dir=ROOT / "infra/helm/postgres",
        application_chart_dir=ROOT / "infra/helm/application",
    )


def prepare(service, run_id, tmp_path, *, app=PODINFO, env_value=ENV_VALUE, **kw):
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={env_value}\nAWS_REGION=ap-northeast-2\n")
    return service.prepare_sandbox(
        requirements(),
        migration_id="it-mig",
        run_id=run_id,
        images=SandboxImages(database=POSTGRES, applications={"demo-api": app}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=["FEATURE_X"],  # AWS_REGION is an excluded variable and is never copied
        env_file=env_file,
        **kw,
    )


def test_a_sandbox_comes_up_passes_its_checks_and_is_removed_completely(service, tmp_path):
    run_id = "it-" + uuid.uuid4().hex[:8]
    plan = prepare(service, run_id, tmp_path)
    try:
        password_uid = kubectl(
            "get",
            "secret",
            f"{plan.releases['demo-db']}-auth",
            "-n",
            plan.namespace,
            "-o",
            "jsonpath={.metadata.uid}",
        ).stdout
        assert password_uid
        deployment = service.apply_sandbox(plan)
        assert set(deployment["endpoints"]) == {"demo-api", "demo-db"}
        report = service.check_sandbox(plan)
        assert report == {"database_version": "16", "sql": "OK", "health": "OK"}

        # The app's own Secret holds only the requested name, and the URL Secret exists.
        env_secret = json.loads(
            kubectl(
                "get",
                "secret",
                f"{plan.releases['demo-api']}-env",
                "-n",
                plan.namespace,
                "-o",
                "json",
            ).stdout
        )
        assert list(env_secret["data"]) == ["FEATURE_X"]
        assert kubectl(
            "get", "secret", f"{plan.releases['demo-db']}-url", "-n", plan.namespace, "-o", "name"
        ).stdout.strip()

        # A retry of the same run must not recreate the password Secret.
        prepare(service, run_id, tmp_path)
        again = kubectl(
            "get",
            "secret",
            f"{plan.releases['demo-db']}-auth",
            "-n",
            plan.namespace,
            "-o",
            "jsonpath={.metadata.uid}",
        ).stdout
        assert again == password_uid

        # No secret value leaks into what the worker hands back.
        handed_back = json.dumps([plan.model_dump(mode="json"), deployment, report])
        assert ENV_VALUE not in handed_back
    finally:
        service.teardown_sandbox(plan.namespace)

    assert (
        kubectl(
            "get", "namespace", plan.namespace, "--ignore-not-found", "-o", "name"
        ).stdout.strip()
        == ""
    )
    leftovers = kubectl("get", "pvc", "-A", "-o", "json")
    assert plan.namespace not in leftovers.stdout


def test_the_dedicated_context_is_the_only_one_the_cluster_accepts():
    from cmp_worker.sandbox_deploy import SandboxError

    with pytest.raises(SandboxError):
        SandboxCluster(
            CommandRunner(), kubeconfig=Path(KUBECONFIG).expanduser(), context="minikube"
        )


def jsonpath(*args):
    return kubectl(*args).stdout.strip()


def sql(plan, statement):
    release = plan.releases["demo-db"]
    script = f'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{statement}"'
    return kubectl("exec", "-n", plan.namespace, f"statefulset/{release}", "--", "sh", "-c", script)


def test_update_changes_only_what_differs_and_reset_restores_the_baseline(service, tmp_path):
    run_id = "up-" + uuid.uuid4().hex[:8]
    first = prepare(service, run_id, tmp_path, app=PODINFO_OLD)
    try:
        service.apply_sandbox(first)
        service.check_sandbox(first)
        ns, db, app = first.namespace, first.releases["demo-db"], first.releases["demo-api"]
        db_pod = jsonpath("get", "pod", f"{db}-0", "-n", ns, "-o", "jsonpath={.metadata.uid}")
        db_rev = jsonpath("get", "secret", "-n", ns, "-l", f"name={db},owner=helm", "-o", "name")
        assert db_pod

        # Baseline first, then data the test run is allowed to change.
        assert service.snapshot_baseline(first) == {"baseline": "SAVED"}
        assert sql(first, "create table scratch(id int)").returncode == 0

        # Image-only change: the database release and pod are untouched.
        second = prepare(service, run_id, tmp_path, app=PODINFO, update_env=True)
        record = service.update_sandbox(first, second, attempt=2)
        assert {k: v.action for k, v in record.resources.items()} == {
            "demo-api": "UPGRADED",
            "demo-db": "UNCHANGED",
        }
        assert record.baseline == "KEPT"
        assert record.resources["demo-api"].image_digest == PODINFO.uri.rsplit("@", 1)[1]
        assert (
            jsonpath("get", "pod", f"{db}-0", "-n", ns, "-o", "jsonpath={.metadata.uid}") == db_pod
        )
        assert (
            jsonpath("get", "secret", "-n", ns, "-l", f"name={db},owner=helm", "-o", "name")
            == db_rev
        )
        service.check_sandbox(second)

        # Changed env value: the application pod is restarted, the database is not.
        app_pods = jsonpath(
            "get", "pod", "-n", ns, "-l", f"app.kubernetes.io/instance={app}", "-o", "name"
        )
        third = prepare(
            service, run_id, tmp_path, app=PODINFO, env_value="rotated-7799", update_env=True
        )
        assert third.env_updated == [f"{app}-env"]
        record = service.update_sandbox(second, third, attempt=3)
        assert record.resources["demo-api"].action == "RESTARTED"
        assert (
            jsonpath(
                "get", "pod", "-n", ns, "-l", f"app.kubernetes.io/instance={app}", "-o", "name"
            )
            != app_pods
        )
        assert (
            jsonpath("get", "pod", f"{db}-0", "-n", ns, "-o", "jsonpath={.metadata.uid}") == db_pod
        )
        assert "rotated-7799" not in json.dumps(
            [third.model_dump(mode="json"), record.model_dump(mode="json")]
        )

        # Reset removes what the run created and keeps what the baseline held.
        assert sql(third, "select count(*) from scratch").stdout.strip() == "0"
        assert service.reset_test_data(third) == {"reset": "OK"}
        assert "does not exist" in sql(third, "select count(*) from scratch").stderr
        service.check_sandbox(third)
    finally:
        service.teardown_sandbox(first.namespace)


def test_a_pinned_suite_runs_in_the_sandbox_and_dependency_checks_use_the_real_database(
    service, tmp_path
):
    from cmp_testsuite import (
        ActionStep,
        CaseResult,
        Expect,
        HttpStep,
        Suite,
        SuiteCase,
        SuiteError,
        dump_bundle,
        run_case,
        run_suite,
    )
    from cmp_testsuite.standard import AppApi, standard_cases
    from cmp_testsuite.validate import validate_tests
    from cmp_worker.artifact_store import LocalArtifactStore

    run_id = "st-" + uuid.uuid4().hex[:8]
    plan = prepare(service, run_id, tmp_path)
    store = LocalArtifactStore(tmp_path / "suite-store")
    try:
        service.apply_sandbox(plan)
        service.check_sandbox(plan)
        survives = SuiteCase(
            id="app-up-after-db-restart", source="STANDARD", effect="READ_ONLY",
            steps=[ActionStep(action="RESTART_DATABASE"), HttpStep(path="/healthz", expect=Expect(status=200))],
        )  # fmt: skip
        info = SuiteCase(
            id="ai-info", source="AI", effect="READ_ONLY",
            steps=[HttpStep(path="/", expect=Expect(status=200, body_contains="podinfo"))],
        )  # fmt: skip
        suite = Suite(
            name="demo", cases=[*standard_cases(AppApi(health_path="/healthz")), survives]
        )
        ref = store.put(dump_bundle(suite), ref_id="suite", kind="test-suite")
        actions = {"RESTART_DATABASE": lambda: service.restart_database(plan)}
        with service.cluster.proxy() as proxy:
            base = proxy + service.service_path(plan, "demo-api")
            result = run_suite(ref, base, store=store, actions=actions)
            assert result.passed, result.results
            assert {r.id: r.status for r in result.results} == {
                "std-health": "PASSED",
                "app-up-after-db-restart": "PASSED",
            }

            # The same pinned suite refuses to run once its bytes change.
            (store.root / ref.sha256).write_bytes(dump_bundle(Suite(name="demo", cases=[info])))
            with pytest.raises(SuiteError):
                run_suite(ref, base, store=store)

            # podinfo does not use the database, so cutting it must not fail the test: refused.
            def target(case):
                return run_case(case, base)

            verdict = validate_tests(
                [info],
                target=target,
                origin=lambda case: CaseResult(id=case.id, status="PASSED"),
                cut_dependency=lambda: service.database_cut(plan),
            ).verdicts[0]
            assert (verdict.accepted, verdict.reason) == (False, "PASSES_WITHOUT_DEPENDENCY")
        service.check_sandbox(plan)  # the database is back after the cut
    finally:
        service.teardown_sandbox(plan.namespace)


def test_security_probes_show_only_the_intents_sources_reach_the_database(service, tmp_path):
    """Ticket 30 on a real cluster: the allowed source connects, an outsider is blocked."""
    from cmp_contracts.provisioning_v2 import RequirementsV2

    base = requirements().model_dump(mode="json")
    base["network_intent"] = [
        {"source_resource_id": "demo-api", "destination_resource_id": "demo-db", "port": 5432}
    ]
    run_id = "pr-" + uuid.uuid4().hex[:8]
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={ENV_VALUE}\n")
    plan = service.prepare_sandbox(
        RequirementsV2.model_validate(base),
        migration_id="it-mig",
        run_id=run_id,
        images=SandboxImages(database=POSTGRES, applications={"demo-api": PODINFO}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=["FEATURE_X"],
        env_file=env_file,
        network=True,
    )
    try:
        service.apply_sandbox(plan)
        service.apply_network(plan)
        service.check_sandbox(plan)
        allowed = service.network_probe(
            plan, role="ALLOWED_CLIENT", source_resource_id="demo-api",
            target_resource_id="demo-db", port=5432,
        )  # fmt: skip
        outsider = service.network_probe(
            plan, role="OUTSIDER", source_resource_id=None, target_resource_id="demo-db", port=5432
        )
        assert (allowed, outsider) == (True, False)
        # The probe Pods and the temporary policy are gone.
        left = kubectl("get", "pods,networkpolicies", "-n", plan.namespace, "-o", "name").stdout
        assert "cmp-probe-" not in left
    finally:
        service.teardown_sandbox(plan.namespace)


CURL = (
    "docker.io/curlimages/curl@sha256:"
    "d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b"
)


class Probe:
    """A long-lived pod that carries a resource's labels, so the policies treat it as that
    resource. It must already be running when traffic starts: the policy engine learns about a
    brand-new pod a moment after it starts, and a throwaway pod would race that."""

    def __init__(self, plan, release, name):
        self.plan, self.name = plan, name
        kubectl(
            "run", name, "-n", plan.namespace, "--restart=Never", f"--image={CURL}",
            f"--labels=app.kubernetes.io/instance={release}", "--command", "--", "sleep", "900",
        )  # fmt: skip
        kubectl(
            "wait", "--for=condition=Ready", f"pod/{name}", "-n", plan.namespace, "--timeout=90s"
        )

    def sh(self, script):
        done = kubectl("exec", "-n", self.plan.namespace, self.name, "--", "sh", "-c", script)
        return done.stdout


def test_network_rules_block_unlisted_traffic_and_record_what_leaves_the_sandbox(service, tmp_path):
    from cmp_contracts.provisioning_v2 import RequirementsV2
    from cmp_testsuite import CaseResult, SuiteResult
    from cmp_worker.sandbox_judge import judge

    base = requirements().model_dump(mode="json")
    base["network_intent"] = [
        {"source_resource_id": "demo-api", "destination_resource_id": "demo-db", "port": 5432},
        {"source_resource_id": "demo-api", "external_host": "example.com", "port": 80},
    ]
    run_id = "nw-" + uuid.uuid4().hex[:8]
    env_file = tmp_path / ".env"
    env_file.write_text(f"FEATURE_X={ENV_VALUE}\n")
    plan = service.prepare_sandbox(
        RequirementsV2.model_validate(base),
        migration_id="it-mig",
        run_id=run_id,
        images=SandboxImages(database=POSTGRES, applications={"demo-api": PODINFO}),
        capabilities=SandboxCapabilities(storage_class="local-path"),
        env_names=["FEATURE_X"],
        env_file=env_file,
        network=True,
    )
    api, db = plan.releases["demo-api"], plan.releases["demo-db"]
    via_proxy = (
        "curl -sS -m 10 -x http://cmp-egress-proxy:3128 -o /dev/null -w 'HTTP:%{http_code}' "
    )
    try:
        service.apply_sandbox(plan)
        service.apply_network(plan)
        app_probe = Probe(plan, api, "probe-app")
        # The runner still reaches the application, so the entry rule works.
        assert service.check_sandbox(plan)["health"] == "OK"

        # Through the proxy: the listed host passes, an unlisted one is refused.
        assert "HTTP:200" in app_probe.sh(via_proxy + "http://example.com/")
        assert "HTTP:403" in app_probe.sh(via_proxy + "http://www.iana.org/")
        # Without the proxy the application cannot leave at all.
        direct = app_probe.sh("curl -sS -m 6 -o /dev/null http://example.com/; echo EXIT:$?")
        assert "EXIT:0" not in direct
        # The application may reach the database (intent), the database may not reach the application.
        to_db = app_probe.sh(f"curl -sv -m 6 telnet://{db}:5432 2>&1 | grep -c Connected; true")
        assert to_db.strip().splitlines()[0] != "0"
        to_app = kubectl(
            "exec", "-n", plan.namespace, f"statefulset/{db}", "--", "bash", "-c",
            f"timeout 5 bash -c '</dev/tcp/{api}/9898'",
        )  # fmt: skip
        assert to_app.returncode != 0

        records = {(r.decision, r.host, r.port) for r in service.egress_records(plan)}
        assert ("ALLOW", "example.com", 80) in records and ("BLOCK", "www.iana.org", 80) in records
        ok = SuiteResult(
            suite_sha256="a" * 64, results=[CaseResult(id="std-health", status="PASSED")]
        )
        verdict = judge(results=ok, egress=service.egress_records(plan), source_hosts={"iana.org"})
        assert not verdict.passed and verdict.reasons == ["MIGRATE_SOURCE_CALLED:www.iana.org:80"]

        # Taking the proxy away makes even listed hosts fail, and it comes back afterwards.
        with service.external_cut(plan):
            assert "HTTP:200" not in app_probe.sh(via_proxy + "http://example.com/")
        assert "HTTP:200" in app_probe.sh(via_proxy + "http://example.com/")
    finally:
        service.teardown_sandbox(plan.namespace)


def _analysis_refs(store):
    """A stored analysis version with one checked row, and the review that approved it."""
    from datetime import UTC, datetime

    from cmp_contracts.analysis import AnalysisReport, MappingRow, ReviewRecord
    from cmp_contracts.provisioning import VersionRef

    row = MappingRow(
        id="r1", capability="environment-config", usage="env settings", grade="DIRECT",
        required_proposed=True, required_reason="needed", required=None, disposition="MIGRATE",
        loses=[], treatment=None, status="EVIDENCE_CHECKED", evidence=[],
    )  # fmt: skip
    ref = VersionRef(id="analysis", version="v1")
    report = AnalysisReport(
        migration_id="it-mig", ref=ref, base_commit="c" * 40, commit_verified=False,
        usage_profile={"usages": []}, mapping=[row], precheck={"findings": []},
        external_entries=[], source_cidrs=[], source_db_images={}, unmapped_items=[],
        unverified_risks=[],
    )  # fmt: skip
    review = ReviewRecord(
        analysis_ref=ref, required={"r1": True}, operational=[], approvals={}, actor="it",
        decided_at=datetime(2026, 11, 5, tzinfo=UTC),
    )  # fmt: skip
    return {
        "analysis_report_ref": store.put_json(report, ref_id="a", kind="analysis-report"),
        "review_ref": store.put_json(review, ref_id="r", kind="review-record"),
    }


def _loop_world(service, tmp_path, *, fixes, build_for=None):
    """Real Sandbox Activities with a fake model and a fake builder, driven through Temporal."""
    from cmp_agents.llm import FakeLLMClient, LLMResponse, call
    from cmp_contracts.verification import canonical_requirements
    from cmp_testsuite import Expect, HttpStep, Suite, SuiteCase, dump_bundle
    from cmp_worker.activities.sandbox_loop import (
        SandboxInput,
        SandboxLoopActivities,
        make_proxy_suite_runner,
    )
    from cmp_worker.artifact_store import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "store")
    builds = []

    def build(resource_id, source):
        builds.append(source)
        return (build_for or (lambda s: PODINFO))(source)

    loop = SandboxLoopActivities(
        service=service,
        store=store,
        workspace_root=tmp_path / "work",
        build=build,
        suite_runner=make_proxy_suite_runner(service),
        llm_factory=lambda *_: FakeLLMClient(
            [LLMResponse(tool_calls=[call(name, **args)]) for name, args in fixes]
        ),
    )
    suite = Suite(
        name="demo",
        cases=[
            SuiteCase(
                id="std-health",
                source="STANDARD",
                effect="READ_ONLY",
                covers=["environment-config"],
                steps=[HttpStep(path="/healthz", expect=Expect(status=200))],
            )
        ],
    )
    return loop, store, builds, SandboxInput, canonical_requirements, dump_bundle(suite)


@pytest.mark.asyncio
async def test_a_wrong_port_is_classified_as_spec_fixed_and_the_same_sandbox_passes(
    service, tmp_path
):
    import os

    from cmp_contracts.provisioning_v2 import RequirementsV2
    from cmp_contracts.queues import WORKFLOW_QUEUE
    from cmp_worker.workers.migration import create_workflow_worker
    from cmp_worker.workers.sandbox import create_worker
    from temporalio.testing import WorkflowEnvironment

    service.helm_timeout = "40s"  # a pod that never becomes ready must fail fast in this test
    fixes = [
        ("submit_requirements_fix", {
            "reason": "the application listens on 9898",
            "edits": [{"resource_id": "demo-api", "field": "port", "value": 9898}],
        })
    ]  # fmt: skip
    loop, store, builds, SandboxInput, canonical, bundle = _loop_world(
        service, tmp_path, fixes=fixes
    )
    wrong = requirements().model_dump(mode="json")
    for resource in wrong["resources"]:
        if resource["id"] == "demo-api":
            resource["port"] = 8080  # the fault: podinfo listens on 9898
    wrong_req = RequirementsV2.model_validate(wrong)
    req_ref = store.put(canonical(wrong_req)[0], ref_id="requirements", kind="requirements")
    suite_ref = store.put(bundle, ref_id="suite", kind="test-suite")
    analysed = _analysis_refs(store)
    source_dir = tmp_path / "src"  # only the application source: results are stored elsewhere
    source_dir.mkdir()
    (source_dir / "main.go").write_text("package main\n")
    data = SandboxInput(
        migration_id="it-mig",
        run_id="lp-" + uuid.uuid4().hex[:8],
        requirements_ref=req_ref,
        suite_ref=suite_ref,
        source={"workspace": str(source_dir)},
        database_image=POSTGRES,
        **analysed,
    ).model_dump(mode="json")
    async with (
        await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=os.environ.get("CMP_TEST_SERVER_PATH")
        ) as env,
        create_worker(env.client, loop),
        create_workflow_worker(env.client),
    ):
        handle = await env.client.start_workflow(
            "SandboxVerificationWorkflow", data, id=f"it-{data['run_id']}",
            task_queue=WORKFLOW_QUEUE,
        )  # fmt: skip
        result = await handle.result()
    assert result["status"] == "PASSED", result
    assert [(a["attempt"], a["passed"], a.get("failure_class")) for a in result["attempts"]] == [
        (1, False, "SPEC"),
        (2, True, None),
    ]
    assert len(builds) == 1  # a requirements fix never rebuilds
    published = result["published"]
    assert published["status"] == "PUBLISHED" and published["patch"] is None
    from cmp_contracts.analysis import AnalysisReport
    from cmp_worker.artifact_store import ArtifactRef

    final = AnalysisReport.model_validate_json(
        store.get(ArtifactRef.model_validate(published["report_ref"]))
    )
    assert [r.status for r in final.mapping] == ["SANDBOX_VERIFIED"]
    assert published["requirements_ref"] == result["requirements_ref"]
    assert result["requirements_ref"] != data["requirements_ref"]
    namespace = f"cmp-sbx-it-mig-{data['run_id']}"
    assert not kubectl("get", "namespace", "-o", "name").stdout.count(data["run_id"]), namespace
