"""ENV-04 (#99): production deploy renders the approved EnvPlan with `render_env`.

Seams: the ticket 18 publisher (SQLite business DB, local ArtifactStore, real Helm render), the
09/11 Activities with their fake clusters (ticket 22 end-to-end), the ticket 12 ON_PREM job
builder + Agent HTTP API + Agent runner double, and `InMemoryEnvFileStore`. No pre-created
ConfigMap or Secret: `<app>-env` and `<db>-conn` come from the `.env` and the target DB outputs.
Value exposure is checked by string search, as in ticket 19.
"""

import base64
import json
import logging

import pytest
import test_plan_publish
from cmp_api.agent_registry import AgentJob
from cmp_api.main import app
from cmp_api.onprem_rollout import RolloutError, build_rollout_job, dispatch, job_env_secrets
from cmp_api.plan_inputs import PlanInputError, input_digest, load_planning_input
from cmp_api.routes.agents import get_artifact_store, get_env_store
from cmp_api.routes.agents import get_session as agent_session
from cmp_contracts.onprem_rollout import ProductionRolloutJob
from cmp_contracts.plan_publish import PlanningInputRegistration
from cmp_providers.env_file import InMemoryEnvFileStore
from cmp_providers.onprem_agent import MANAGED, RolloutAgent, RolloutRunner
from cmp_providers.plans import approval_issues
from cmp_providers.remote_agent import AgentTransportError
from cmp_worker.activities import aws_workloads, gcp_workloads
from fastapi.testclient import TestClient
from onprem_rollout_support import register_agent
from temporalio.testing import ActivityEnvironment
from test_aws_workloads import FakeCluster as AwsCluster
from test_aws_workloads import FakeRuntime as AwsRuntime
from test_cloud_workload_plan import (
    AWS_REFS,
    GCP_REFS,
    INFRA_STEP,
    WORKLOADS,
    approve,
    cloud_publisher,
    finish,
)
from test_gcp_workloads import FakeCluster as GcpCluster
from test_gcp_workloads import FakeRuntime as GcpRuntime
from test_onprem_agent import CONTENT, FakeClient, fetch, job
from test_plan_publish import MIGRATION, publisher, register, registration, revisions, stored

db, store = test_plan_publish.db, test_plan_publish.store
platform_registry = test_plan_publish.platform_registry  # ticket 29: autouse here too

NAMESPACE = WORKLOADS["namespace"]  # "cmp-prod-demo" for both ON_PREM and cloud targets
ENV_TEXT = (
    b"APP_MODE=prod\n"
    b"API_TOKEN=tok-SECRET-123\n"
    b"AWS_SECRET_ACCESS_KEY=aws-SECRET-456\n"
    b"DB_HOST=old-db.internal\n"
    b"DB_PASSWORD=old-SECRET-pw\n"
    b"DATABASE_URL=postgres://old-SECRET-url\n"
)
VALUES = ["tok-SECRET-123", "aws-SECRET-456", "old-db.internal", "old-SECRET-pw", "old-SECRET-url"]
APP_ENV = {"API_TOKEN": "tok-SECRET-123", "APP_MODE": "prod"}
CONN = {
    "host": f"postgres-main.{NAMESPACE}.svc",
    "port": "5432",
    "name": "appdb",
    "username": "appuser",
}


def entries(**override):
    base = {
        "APP_MODE": {"disposition": "PASS"},
        "API_TOKEN": {"disposition": "PASS"},
        "AWS_SECRET_ACCESS_KEY": {"disposition": "EXCLUDED", "reason": "SOURCE_ONLY_CREDENTIAL"},
        "DB_HOST": {"disposition": "REPLACED", "binding": "host"},
        "DB_PASSWORD": {"disposition": "REPLACED", "binding": "password_secret_ref"},
        "DATABASE_URL": {"disposition": "REPLACED", "binding": "connection_url_secret_ref"},
        **override,
    }
    return [{"key": key, **entry} for key, entry in base.items() if entry is not None]


def env_registration(
    env_store, provider="ON_PREM", *, content=ENV_TEXT, plan_ref=None, file_id=None, **override
):
    ref = env_store.put(content, file_id=file_id).model_dump(mode="json")
    _, body = registration(provider)
    body["planning_request"]["environment"]["env_file_ref"] = ref
    if provider != "ON_PREM":
        body["target"]["workloads"] = WORKLOADS
    body["env_plan"] = {"env_file_ref": plan_ref or ref, "entries": entries(**override)}
    return PlanningInputRegistration.model_validate(body)


def assert_no_values(tmp_path, *texts):
    """No `.env` value in any file the platform wrote (ArtifactStore, Plan folders, SQLite
    business DB) nor in the given results/logs."""
    blobs = [p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()]
    for value in VALUES:
        assert all(value.encode() not in blob for blob in blobs), value
        assert all(value not in text for text in texts), value


@pytest.fixture
def env_store():
    return InMemoryEnvFileStore()


def sources(store, manifest):
    values = json.loads(store.read_raw(manifest.artifacts["application_values"].sha256))
    return {item["name"]: item["source"] for item in values["env"]}


# --- ON_PREM Plan publication ----------------------------------------------------------------


def publish_onprem(db, store, tmp_path, env_store, **kwargs):
    with db() as session:
        credential = register_agent(session)
    register(db, store, env_registration(env_store, **kwargs))
    return publisher(db, store, tmp_path, env_store=env_store).publish(MIGRATION), credential


def test_onprem_plan_wires_env_through_secret_refs_and_shows_names_only(
    db, store, tmp_path, env_store
):
    result, _ = publish_onprem(db, store, tmp_path, env_store)
    assert result["status"] == "PUBLISHED", result
    manifest, preview = stored(db, result)
    assert approval_issues(manifest, preview) == []
    assert sources(store, manifest) == {
        "APP_MODE": {"kind": "SECRET", "name": "api-main-env", "key": "APP_MODE"},
        "API_TOKEN": {"kind": "SECRET", "name": "api-main-env", "key": "API_TOKEN"},
        "DB_HOST": {"kind": "SECRET", "name": "postgres-main-conn", "key": "host"},
        "DB_PASSWORD": {"kind": "SECRET", "name": "postgres-main-auth", "key": "password"},
        "DATABASE_URL": {"kind": "SECRET", "name": "postgres-main-url", "key": "url"},
    }
    spec = json.loads(store.read(manifest.artifacts["spec"]))
    assert spec["env_plan"]["env_file_ref"] == spec["spec"]["environment"]["env_file_ref"]
    # Platform-made Secrets are not asked of the user; the Preview lists keys, never values.
    assert all(w.code != "USER_SECRET_REQUIRED" for w in preview.warnings)
    shown = " ".join(f"{c.name} {c.size}" for c in preview.applied_defaults)
    for word in ("api-main-env", "API_TOKEN", "APP_MODE", "postgres-main-conn"):
        assert word in shown
    assert "AWS_SECRET_ACCESS_KEY" in shown and "DB_HOST→host" in shown
    assert env_store.read_values_calls == 0  # Plan time reads key names only
    assert_no_values(tmp_path, json.dumps(result), preview.model_dump_json())


def test_decisions_are_published_for_review_but_block_approval(db, store, tmp_path, env_store):
    result, _ = publish_onprem(
        db,
        store,
        tmp_path,
        env_store,
        DB_DSN={"disposition": "UNREPRESENTABLE", "reason": "DSN_FORMAT"},
        FEATURE_FLAG={"disposition": "MISSING", "reason": "NOT_DECLARED"},
    )
    assert result["status"] == "PUBLISHED", result
    manifest, preview = stored(db, result)
    assert [(i.code, i.path) for i in preview.pending_decisions] == [
        ("ENV_UNREPRESENTABLE", "environment.env_plan.DB_DSN"),
        ("ENV_MISSING", "environment.env_plan.FEATURE_FLAG"),
    ]
    assert {i.code for i in approval_issues(manifest, preview)} == {"PENDING_DECISION"}
    assert env_store.read_values_calls == 0


def test_pass_key_without_a_value_in_the_file_is_a_decision(db, store, tmp_path, env_store):
    content = ENV_TEXT.replace(b"API_TOKEN=tok-SECRET-123\n", b"")
    result, _ = publish_onprem(db, store, tmp_path, env_store, content=content)
    manifest, preview = stored(db, result)
    assert [i.code for i in preview.pending_decisions] == ["ENV_VALUE_MISSING"]
    assert {i.code for i in approval_issues(manifest, preview)} == {"PENDING_DECISION"}


def test_unusable_store_or_file_is_not_published(db, store, tmp_path, env_store):
    with db() as session:
        register_agent(session)
    reg = env_registration(env_store)
    register(db, store, reg)
    unconfigured = publisher(db, store, tmp_path).publish(MIGRATION)
    assert unconfigured == {
        "status": "FAILED",
        "code": "ENV_FILE_STORE_UNCONFIGURED",
        "issues": ["ENV_FILE_STORE_UNCONFIGURED"],
    }
    env_store.expire(reg.planning_request.environment.env_file_ref)
    expired = publisher(db, store, tmp_path, env_store=env_store).publish(MIGRATION)
    assert (expired["code"], expired["issues"]) == ("ENV_FILE_UNAVAILABLE", ["ENV_FILE_EXPIRED"])
    assert revisions(db) == 0


def test_env_plan_must_refer_to_the_planning_env_file(db, store, env_store):
    other = env_store.put(b"OTHER=1\n").model_dump(mode="json")
    with pytest.raises(PlanInputError) as caught:
        register(db, store, env_registration(env_store, plan_ref=other))
    assert caught.value.code == "ENV_PLAN_REF_MISMATCH"


def test_requirements_binding_contradicting_the_env_plan_is_not_published(
    db, store, tmp_path, env_store
):
    result, _ = publish_onprem(db, store, tmp_path, env_store, DB_HOST={"disposition": "PASS"})
    assert (result["code"], result["issues"]) == (
        "PLANNING_INPUT_INVALID",
        ["ENV_PLAN_BINDING_MISMATCH"],
    )


def test_env_plan_feeds_only_the_digest_of_the_plan_that_renders_it(env_store):
    plain = registration()[0]
    assert "env_plan" not in plain.model_dump(mode="json")  # stored bytes unchanged
    onprem = env_registration(env_store)
    assert input_digest(onprem) != input_digest(onprem.model_copy(update={"env_plan": None}))
    aws = env_registration(env_store, "AWS")  # infrastructure is not re-planned
    assert input_digest(aws) == input_digest(aws.model_copy(update={"env_plan": None}))


# --- ON_PREM rollout: job body, Agent secrets API ----------------------------------------------


@pytest.fixture
def client(db, store, env_store):
    def session():
        with db() as value:
            yield value

    app.dependency_overrides[agent_session] = session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_env_store] = lambda: env_store
    yield TestClient(app)
    app.dependency_overrides.clear()


def dispatched(db, store, published):
    claim = approve(db, published)
    with db() as session:
        built, owner = build_rollout_job(session, claim, store)
        dispatch(session, built, owner)
    return claim, built


def test_rollout_job_names_env_secrets_and_the_agent_api_returns_them(
    db, store, tmp_path, env_store, client
):
    published, credential = publish_onprem(db, store, tmp_path, env_store)
    _, built = dispatched(db, store, published)
    assert [s.model_dump() for s in built.application.env_secrets] == [
        {"name": "api-main-env", "keys": ["API_TOKEN", "APP_MODE"]},
        {"name": "postgres-main-conn", "keys": ["host"]},
    ]
    assert built.application.secret_refs == []  # nothing the user must pre-create
    headers = {"Authorization": f"Bearer {credential}"}
    claimed = client.post("/agents/agent-a/jobs/claim", headers=headers)
    job_id = claimed.json()["job"]["job_id"]
    path = f"/agents/agent-a/jobs/{job_id}/env-secrets"
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "secrets": [
            {"name": "api-main-env", "data": APP_ENV},
            {"name": "postgres-main-conn", "data": CONN},
        ]
    }
    assert client.get(path).status_code == 401
    with db() as session:
        other = register_agent(session, agent_id="agent-b", cluster_uid="cluster-b")
    other_headers = {"Authorization": f"Bearer {other}"}
    assert client.get(path, headers=other_headers).status_code == 403
    assert (
        client.get(f"/agents/agent-b/jobs/{job_id}/env-secrets", headers=other_headers).status_code
        == 404
    )
    with db() as session:
        payload = session.get(AgentJob, job_id).payload
    assert_no_values(tmp_path, json.dumps(payload), claimed.text)


def test_agent_api_refuses_unless_running_and_reports_store_codes(db, store, tmp_path, env_store):
    published, _ = publish_onprem(db, store, tmp_path, env_store)
    _, built = dispatched(db, store, published)
    with db() as session:
        agent_job = session.get(AgentJob, built.job_id)
        with pytest.raises(RolloutError) as queued:
            job_env_secrets(session, agent_job, store, env_store)
        assert queued.value.code == "ENV_SECRETS_NOT_FOUND"  # QUEUED, not claimed
        agent_job.status = "RUNNING"
        session.commit()
        assert len(job_env_secrets(session, agent_job, store, env_store)) == 2
        env_store.expire(env_file_ref(db))
        with pytest.raises(RolloutError) as expired:
            job_env_secrets(session, agent_job, store, env_store)
        assert expired.value.code == "ENV_FILE_EXPIRED"


def env_file_ref(db):
    with db() as session:
        return load_planning_input(session, MIGRATION).env_plan.env_file_ref


def test_new_env_version_after_approval_makes_the_approved_revision_stale(
    db, store, tmp_path, env_store
):
    published, _ = publish_onprem(db, store, tmp_path, env_store, file_id="env-a")
    claim = approve(db, published)
    register(db, store, env_registration(env_store, file_id="env-a"))  # version 2
    again = publisher(db, store, tmp_path, env_store=env_store).publish(MIGRATION)
    assert (again["code"], again["revision"]) == ("PLAN_PUBLISHED", 2)
    with db() as session, pytest.raises(RolloutError) as caught:
        build_rollout_job(session, claim, store)
    assert caught.value.code == "STALE_REVISION"


def test_onprem_plan_without_env_plan_keeps_its_job_bytes(db, store, tmp_path):
    with db() as session:
        register_agent(session)
    register(db, store, registration()[0])
    published = publisher(db, store, tmp_path).publish(MIGRATION)
    _, built = dispatched(db, store, published)
    assert "env_secrets" not in built.model_dump(mode="json")["application"]


# --- ON_PREM Agent runner (ticket 12 cluster double) -----------------------------------------


def env_job():
    application = job().application.model_dump(mode="json") | {
        "secret_refs": [],
        "env_secrets": [
            {"name": "demo-app-env", "keys": ["API_TOKEN"]},
            {"name": "demo-db-conn", "keys": ["host"]},
        ],
    }
    return job(application=application)


def rendered():
    return [
        {"name": "demo-app-env", "data": {"API_TOKEN": "tok-SECRET-123"}},
        {"name": "demo-db-conn", "data": {"host": "demo-db.demo.svc", "port": "5432"}},
    ]


def agent_cluster():
    from test_onprem_agent import FakeCluster

    cluster = FakeCluster()
    del cluster.objects[("secret", "demo-app-env")]  # nothing pre-created by a person
    return cluster


def decoded(cluster, name):
    secret = cluster.objects[("secret", name)]
    assert secret["metadata"]["labels"] == {MANAGED: "true"}
    return {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}


def test_agent_applies_fetched_env_secrets_before_helm():
    cluster = agent_cluster()
    assert RolloutRunner(cluster, fetch, rendered).rollout(env_job()) == ("SUCCEEDED", "READY")
    assert decoded(cluster, "demo-app-env") == {"API_TOKEN": "tok-SECRET-123"}
    assert decoded(cluster, "demo-db-conn")["host"] == "demo-db.demo.svc"
    # password Secret, then the two EnvPlan Secrets, then the two Helm Releases
    # (the data migration RoleBinding comes first: tests/test_onprem_agent.py covers it)
    assert cluster.mutations[1:4] == [("apply", "Secret")] * 3
    assert [m for m in cluster.mutations if m[0] == "helm"] == [
        ("helm", "demo-db"),
        ("helm", "demo-app"),
    ]


def test_agent_refreshes_its_own_secret_but_never_a_user_secret():
    cluster = agent_cluster()
    cluster.objects[("secret", "demo-app-env")] = {
        "metadata": {"labels": {MANAGED: "true"}},
        "data": {"API_TOKEN": "b2xk"},
    }
    assert RolloutRunner(cluster, fetch, rendered).rollout(env_job())[1] == "READY"
    assert decoded(cluster, "demo-app-env") == {"API_TOKEN": "tok-SECRET-123"}

    user = agent_cluster()
    user.objects[("secret", "demo-db-conn")] = {"data": {"host": "dXNlcg=="}}
    result = RolloutRunner(user, fetch, rendered).rollout(env_job())
    assert result == ("FAILED", "SECRET_NAME_CONFLICT") and user.mutations == []


def unavailable():
    raise AgentTransportError("PLATFORM_REQUEST_FAILED")


@pytest.mark.parametrize(
    ("fetcher", "expected"),
    [
        (None, ("FAILED", "ENV_SECRETS_NOT_CONFIGURED")),
        (unavailable, ("UNKNOWN", "ENV_SECRETS_UNAVAILABLE")),
        (lambda: rendered()[:1], ("FAILED", "ENV_SECRETS_INVALID")),
        (
            lambda: [rendered()[0], {"name": "demo-db-conn", "data": {}}],
            ("FAILED", "ENV_SECRETS_INVALID"),
        ),
    ],
)
def test_agent_changes_nothing_without_the_expected_env_secrets(fetcher, expected):
    cluster = agent_cluster()
    assert RolloutRunner(cluster, fetch, fetcher).rollout(env_job()) == expected
    assert cluster.mutations == []


class EnvClient(FakeClient):
    def env_secrets(self, *, agent_id, credential, job_id):
        return rendered()


def test_agent_daemon_fetches_env_secrets_and_reports_codes_only():
    client = EnvClient(env_job().model_dump(mode="json"))
    cluster = agent_cluster()
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    [report] = client.reports
    assert (report["status"], report["code"]) == ("SUCCEEDED", "READY")
    assert "tok-SECRET-123" not in json.dumps(report)
    assert CONTENT  # artifacts came from the same fake transport


def test_job_without_env_secrets_keeps_its_bytes():
    assert "env_secrets" not in job().model_dump(mode="json")["application"]
    ProductionRolloutJob.model_validate(env_job().model_dump(mode="json"))


# --- AWS/GCP: infra result → WORKLOADS Plan → 09/11 deploy (ticket 22 end-to-end) ---------------


class ManagedSecrets:
    """`secret_managed`/`apply_secret`/`secret_value` with platform ownership (ticket 19)."""

    def __init__(self):
        super().__init__()
        self.managed: set[tuple[str, str]] = set()

    def create_secret(self, namespace, name, data):
        super().create_secret(namespace, name, data)
        self.managed.add((namespace, name))

    def secret_managed(self, namespace, name):
        if (namespace, name) not in self.secrets:
            return None
        return (namespace, name) in self.managed

    def secret_value(self, namespace, name, key):
        return self.secrets.get((namespace, name), {}).get(key)

    def apply_secret(self, namespace, name, *args):
        data = args[-1]
        self.secrets[(namespace, name)] = dict(data)
        self.managed.add((namespace, name))


class EnvAwsCluster(ManagedSecrets, AwsCluster):
    pass


class EnvGcpCluster(ManagedSecrets, GcpCluster):
    pass


def env_workload_activity(db, tmp_path, provider, env_store):
    if provider == "AWS":
        cluster = EnvAwsCluster()
        activities = aws_workloads.AwsWorkloadsActivities(
            db,
            artifact_root=tmp_path / "plans",
            resolver=aws_workloads.ExecutionRefsResolver(),
            runtime=AwsRuntime(cluster, account="123456789012"),
            timeouts=aws_workloads.Timeouts(readiness_seconds=0, alb_seconds=0, interval_seconds=0),
            env_store=env_store,
        )
        return cluster, activities.execute_aws_workloads
    cluster = EnvGcpCluster()
    runtime = GcpRuntime(cluster)
    runtime.project, runtime.aws_account = "sbh26-test", "123456789012"
    activities = gcp_workloads.GcpWorkloadsActivities(
        db,
        artifact_root=tmp_path / "plans",
        resolver=gcp_workloads.ExecutionRefsResolver(),
        runtime=runtime,
        timeouts=gcp_workloads.Timeouts(readiness_seconds=0, ingress_seconds=0, interval_seconds=0),
        env_store=env_store,
    )
    return cluster, activities.execute_gcp_workloads


def cloud_workloads_published(db, store, tmp_path, monkeypatch, env_store, provider, **kwargs):
    register(db, store, env_registration(env_store, provider, **kwargs))
    pub = cloud_publisher(db, store, tmp_path, monkeypatch, provider)
    pub._env_store = env_store  # the worker wiring passes the configured platform store
    infra = pub.publish(MIGRATION)
    finish(
        db,
        approve(db, infra),
        refs=AWS_REFS if provider == "AWS" else GCP_REFS,
        step=INFRA_STEP[provider],
    )
    ref = {"plan_id": infra["plan_id"], "revision": infra["revision"]}
    published = pub.publish_workloads(MIGRATION, ref)
    assert published["status"] == "PUBLISHED", published
    return pub, ref, published


@pytest.mark.parametrize("provider", ["AWS", "GCP"])
async def test_cloud_deploy_creates_env_and_conn_secrets_with_no_precreated_refs(
    db, store, tmp_path, monkeypatch, env_store, provider, caplog
):
    caplog.set_level(logging.DEBUG)
    _, _, published = cloud_workloads_published(
        db, store, tmp_path, monkeypatch, env_store, provider
    )
    manifest, preview = stored(db, published)
    assert approval_issues(manifest, preview) == []
    folder = tmp_path / "plans" / f"{published['plan_id']}-r{published['revision']}"
    values = json.loads((folder / manifest.artifacts["application_values"].sha256).read_bytes())
    assert {e["name"]: e["source"] for e in values["env"]} == {
        "APP_MODE": {"kind": "USER_SECRET", "name": "api-main-env", "key": "APP_MODE"},
        "API_TOKEN": {"kind": "USER_SECRET", "name": "api-main-env", "key": "API_TOKEN"},
        "DB_HOST": {"kind": "USER_SECRET", "name": "postgres-main-conn", "key": "host"},
        "DB_PASSWORD": {"kind": "DATABASE", "field": "secret_ref"},
        "DATABASE_URL": {"kind": "DATABASE", "field": "connection_url_secret_ref"},
    }

    claim = approve(db, published)
    cluster, execute = env_workload_activity(db, tmp_path, provider, env_store)
    cluster.secrets.clear()  # no ConfigMap or Secret made by a person
    cluster.configmaps.clear()
    result = await ActivityEnvironment().run(execute, {**claim, "resource_refs": {}})
    assert (result["status"], result["code"]) == ("SUCCEEDED", "WORKLOADS_READY"), result
    assert cluster.secrets[(NAMESPACE, "api-main-env")] == APP_ENV
    assert cluster.secrets[(NAMESPACE, "postgres-main-conn")] == CONN
    assert (NAMESPACE, "api-main-env") in cluster.managed
    assert result["resource_refs"]["env_secrets"] == "api-main-env,postgres-main-conn"
    app_values = next(v for _, name, v in cluster.helm_calls if name == "api-main")
    refs = {e["name"]: e["source"] for e in app_values["env"]}
    assert refs["API_TOKEN"] == {"kind": "USER_SECRET", "name": "api-main-env", "key": "API_TOKEN"}
    assert app_values["database"]["url_secret_name"] == "postgres-main-url"
    assert_no_values(
        tmp_path, json.dumps(result), json.dumps(published), "\n".join(cluster.trace), caplog.text
    )


async def test_unmanaged_secret_with_the_env_name_is_never_overwritten(
    db, store, tmp_path, monkeypatch, env_store
):
    _, _, published = cloud_workloads_published(db, store, tmp_path, monkeypatch, env_store, "AWS")
    claim = approve(db, published)
    cluster, execute = env_workload_activity(db, tmp_path, "AWS", env_store)
    cluster.secrets[(NAMESPACE, "api-main-env")] = {"API_TOKEN": "user-owned"}
    result = await ActivityEnvironment().run(execute, {**claim, "resource_refs": {}})
    assert (result["status"], result["code"]) == ("FAILED", "SECRET_NAME_CONFLICT")
    assert cluster.secrets[(NAMESPACE, "api-main-env")] == {"API_TOKEN": "user-owned"}
    assert (NAMESPACE, "postgres-main-conn") not in cluster.secrets
    assert cluster.helm_calls == []


@pytest.mark.parametrize("case", ["expired", "unconfigured", "tampered"])
async def test_deploy_stops_before_helm_when_the_env_cannot_be_rendered(
    db, store, tmp_path, monkeypatch, env_store, case
):
    _, _, published = cloud_workloads_published(db, store, tmp_path, monkeypatch, env_store, "AWS")
    claim = approve(db, published)
    ref = env_file_ref(db)
    if case == "expired":
        env_store.expire(ref)
    elif case == "tampered":  # stored bytes no longer match the approved sha256
        env_store._files[(ref.id, ref.version)] = ("0" * 64, "API_TOKEN=x\n")
    cluster, execute = env_workload_activity(
        db, tmp_path, "AWS", None if case == "unconfigured" else env_store
    )
    result = await ActivityEnvironment().run(execute, {**claim, "resource_refs": {}})
    expected = {
        "expired": "ENV_FILE_EXPIRED",
        "unconfigured": "ENV_FILE_STORE_UNCONFIGURED",
        "tampered": "ENV_FILE_DIGEST_MISMATCH",
    }[case]
    assert (result["status"], result["code"]) == ("FAILED", expected)
    assert cluster.helm_calls == []


async def test_cloud_deploy_uses_only_the_approved_env_version(
    db, store, tmp_path, monkeypatch, env_store
):
    """ENV-001: a later upload is a new version; the approved Plan keeps the approved one.
    (Re-registering it changes the Planning input → new Revisions → the old approval is stale,
    as the ON_PREM test above shows.)"""
    _, _, published = cloud_workloads_published(
        db, store, tmp_path, monkeypatch, env_store, "AWS", file_id="env-a"
    )
    claim = approve(db, published)
    env_store.put(ENV_TEXT.replace(b"tok-SECRET-123", b"tok-NEW"), file_id="env-a")
    cluster, execute = env_workload_activity(db, tmp_path, "AWS", env_store)
    result = await ActivityEnvironment().run(execute, {**claim, "resource_refs": {}})
    assert result["code"] == "WORKLOADS_READY"
    assert cluster.secrets[(NAMESPACE, "api-main-env")]["API_TOKEN"] == "tok-SECRET-123"
