"""Ticket 29 (#153): platform-minted ECR pull credentials for K3s (Agent) and GKE.

Seams: `EcrTokenMinter` with a botocore Stubber ECR client; the ticket 12 ON_PREM Revision +
FastAPI Agent API with an injected fake minter; the ticket 12 Agent cluster double; the
ticket 11 GKE runtime/cluster doubles. The token value is checked by string search.
"""

import base64
from datetime import UTC, datetime
from subprocess import CompletedProcess

import boto3
import pytest
from botocore.stub import Stubber
from cmp_contracts.onprem_rollout import ProductionRolloutJob
from cmp_providers.ecr_pull import (
    EcrPullError,
    EcrTokenMinter,
    PlatformRegistry,
)

ACCOUNT = "123456789012"
REGISTRY = f"{ACCOUNT}.dkr.ecr.ap-northeast-1.amazonaws.com"
IMAGE = f"{REGISTRY}/cmp-platform-builds@sha256:" + "a" * 64
TOKEN = "ecr-SECRET-token-value"
EXPIRES = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def platform(**override):
    values = {"registry": REGISTRY, "repositories": "cmp-platform-builds"} | override
    return PlatformRegistry.parse(values["registry"], values["repositories"])


# --- I1 registry configuration -----------------------------------------------------------------


def test_registry_is_required_only_for_ecr_images_of_the_platform_repository():
    registry = platform()
    assert registry.required([IMAGE, "docker.io/library/postgres@sha256:" + "b" * 64]) == REGISTRY
    assert registry.required(["registry.example.com/demo@sha256:" + "a" * 64]) is None
    other_repo = f"{REGISTRY}/someone-else@sha256:" + "a" * 64
    other_account = "999999999999.dkr.ecr.ap-northeast-1.amazonaws.com/cmp-platform-builds:x"
    for uri in (other_repo, other_account):
        with pytest.raises(EcrPullError) as refused:
            registry.required([uri])
        assert refused.value.code == "IMAGE_REGISTRY_NOT_ALLOWED"


def test_unset_registry_refuses_ecr_images_and_keeps_other_images():
    assert PlatformRegistry.parse(None, None) is None
    assert PlatformRegistry.require(None, ["registry.example.com/demo:1"]) is None
    with pytest.raises(EcrPullError) as refused:
        PlatformRegistry.require(None, [IMAGE])
    assert refused.value.code == "IMAGE_REGISTRY_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("registry", "repositories"),
    [("example.com", "cmp-platform-builds"), (REGISTRY, ""), (f"https://{REGISTRY}", "x")],
)
def test_invalid_registry_configuration_is_refused(registry, repositories):
    with pytest.raises(EcrPullError) as refused:
        PlatformRegistry.parse(registry, repositories)
    assert refused.value.code == "PLATFORM_REGISTRY_INVALID"


# --- I2 token minting --------------------------------------------------------------------------


def stubbed(response=None, error=None):
    client = boto3.client(
        "ecr",
        region_name="ap-northeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    stub = Stubber(client)
    if error:
        stub.add_client_error("get_authorization_token", service_error_code=error)
    else:
        stub.add_response("get_authorization_token", response, {})
    stub.activate()
    return client, stub


def token_response(endpoint=f"https://{REGISTRY}"):
    encoded = base64.b64encode(f"AWS:{TOKEN}".encode()).decode()
    return {
        "authorizationData": [
            {"authorizationToken": encoded, "expiresAt": EXPIRES, "proxyEndpoint": endpoint}
        ]
    }


def test_minter_returns_a_credential_for_the_platform_registry_without_leaking_it():
    client, stub = stubbed(token_response())
    regions = []

    def factory(region):
        regions.append(region)
        return client

    credential = EcrTokenMinter(factory).mint(platform())
    stub.assert_no_pending_responses()
    assert regions == ["ap-northeast-1"]
    assert (credential.registry, credential.username) == (REGISTRY, "AWS")
    assert credential.expires_at == int(EXPIRES.timestamp())
    assert credential.reveal() == TOKEN
    assert TOKEN not in repr(credential) and TOKEN not in str(credential)
    body = credential.response()
    assert body == {
        "registry": REGISTRY,
        "username": "AWS",
        "password": TOKEN,
        "expires_at": int(EXPIRES.timestamp()),
    }


def test_minter_refuses_a_token_for_another_account_and_hides_aws_errors():
    client, _ = stubbed(token_response("https://999999999999.dkr.ecr.ap-northeast-1.amazonaws.com"))
    with pytest.raises(EcrPullError) as other:
        EcrTokenMinter(lambda region: client).mint(platform())
    assert other.value.code == "ECR_ACCOUNT_MISMATCH"
    client, _ = stubbed(error="AccessDeniedException")
    with pytest.raises(EcrPullError) as denied:
        EcrTokenMinter(lambda region: client).mint(platform())
    assert denied.value.code == "ECR_TOKEN_UNAVAILABLE"
    assert denied.value.__cause__ is None and denied.value.__suppress_context__


# --- I3/I4 ON_PREM job and Agent API --------------------------------------------------------------

import json

from cmp_api.agent_registry import AgentJob, poll_job, report_job
from cmp_api.db import Base
from cmp_api.main import app
from cmp_api.onprem_rollout import (
    LocalArtifactStore,
    RolloutError,
    build_rollout_job,
    dispatch,
)
from cmp_api.routes.agents import get_artifact_store, get_ecr_minter
from cmp_api.routes.agents import get_session as agent_session
from cmp_providers.ecr_pull import PullCredential
from fastapi.testclient import TestClient
from onprem_rollout_support import (
    approve,
    build_artifacts,
    claim_request,
    plan,
    register_agent,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'business.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("pull-artifacts")
    store = LocalArtifactStore(root / "store")
    return store, {
        "ecr": build_artifacts(store, root, app_image=IMAGE),
        "public": build_artifacts(store, root),
        "foreign": build_artifacts(
            store, root, app_image=f"{REGISTRY}/someone-else@sha256:" + "a" * 64
        ),
    }


@pytest.fixture
def store(built):
    return built[0]


@pytest.fixture
def configured(monkeypatch):
    from cmp_api.config import settings

    monkeypatch.setattr(settings, "platform_ecr_registry", REGISTRY)
    monkeypatch.setattr(settings, "platform_ecr_repositories", "cmp-platform-builds")


class FakeMinter:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def mint(self, registry):
        self.calls.append(registry.registry)
        if self.error:
            raise EcrPullError(self.error)
        return PullCredential(registry.registry, "AWS", 1_800_000_000, TOKEN)


CREDENTIALS = {}


def queued(db, store, artifacts, revision=1):
    with db() as session:
        if revision == 1:
            CREDENTIALS["agent-a"] = register_agent(session)
        manifest = plan(artifacts, revision=revision)
        record = approve(session, manifest)
        job, owner = build_rollout_job(session, claim_request(record, manifest), store)
        dispatch(session, job, owner)
    return job


def headers(agent="agent-a"):
    return {"Authorization": f"Bearer {CREDENTIALS[agent]}"}


def test_ecr_image_job_names_the_pull_secret_and_public_job_keeps_its_bytes(
    db, store, built, configured
):
    job = queued(db, store, built[1]["ecr"])
    assert job.model_dump(mode="json")["image_pull"] == {
        "secret": "cmp-ecr-pull",
        "registry": REGISTRY,
    }


def test_public_images_need_no_pull_secret_or_registry_setting(db, store, built):
    job = queued(db, store, built[1]["public"])
    assert job.image_pull is None and "image_pull" not in job.model_dump(mode="json")


@pytest.mark.parametrize("artifact", ["ecr", "foreign"])
def test_ecr_image_outside_the_platform_registry_is_refused_before_queueing(
    db, store, built, artifact, monkeypatch
):
    from cmp_api.config import settings

    if artifact == "foreign":
        monkeypatch.setattr(settings, "platform_ecr_registry", REGISTRY)
    with pytest.raises(RolloutError) as refused:
        queued(db, store, built[1][artifact])
    assert refused.value.code == "IMAGE_REGISTRY_NOT_ALLOWED"
    with db() as session:
        assert session.query(AgentJob).count() == 0


@pytest.fixture
def client(db, store):
    def session():
        with db() as value:
            yield value

    minter = FakeMinter()
    app.dependency_overrides[agent_session] = session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_ecr_minter] = lambda: minter
    yield TestClient(app), minter
    app.dependency_overrides.clear()


def claim(db, agent="agent-a"):
    with db() as session:
        return poll_job(session, agent_id=agent, credential=CREDENTIALS[agent])


def finish(db, job_id, status="SUCCEEDED", code="READY"):
    with db() as session:
        report_job(
            session,
            agent_id="agent-a",
            credential=CREDENTIALS["agent-a"],
            job_id=job_id,
            status=status,
            result={"code": code},
        )


def test_agent_gets_a_token_for_its_running_and_live_job_only(db, store, built, configured, client):
    http, minter = client
    job = queued(db, store, built[1]["ecr"])
    path = f"/agents/agent-a/jobs/{job.job_id}/image-pull"
    assert http.get(path, headers=headers()).status_code == 404  # QUEUED, not claimed yet
    claimed = claim(db)
    response = http.get(path, headers=headers())
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "registry": REGISTRY,
        "username": "AWS",
        "password": TOKEN,
        "expires_at": 1_800_000_000,
    }
    assert minter.calls == [REGISTRY]
    finish(db, job.job_id)
    assert http.get(path, headers=headers()).status_code == 200  # live deployment: refresh
    with db() as session:
        stored = session.get(AgentJob, job.job_id)
        assert TOKEN not in json.dumps([stored.payload, stored.result, claimed["payload"]])

    assert http.get(path).status_code == 401
    with db() as session:
        CREDENTIALS["agent-b"] = register_agent(
            session, agent_id="agent-b", cluster_uid="cluster-b"
        )
    assert http.get(path, headers=headers("agent-b")).status_code == 403
    foreign = f"/agents/agent-b/jobs/{job.job_id}/image-pull"
    assert http.get(foreign, headers=headers("agent-b")).status_code == 404
    assert minter.calls == [REGISTRY, REGISTRY]


def test_refresh_stops_once_a_newer_revision_is_approved(db, store, built, configured, client):
    http, minter = client
    job = queued(db, store, built[1]["ecr"])
    claim(db)
    finish(db, job.job_id)
    queued(db, store, built[1]["ecr"], revision=2)
    response = http.get(f"/agents/agent-a/jobs/{job.job_id}/image-pull", headers=headers())
    assert (response.status_code, response.json()["detail"]) == (404, "IMAGE_PULL_NOT_FOUND")
    assert minter.calls == []


def test_failed_job_and_public_job_get_no_token(db, store, built, configured, client):
    http, minter = client
    job = queued(db, store, built[1]["ecr"])
    claim(db)
    finish(db, job.job_id, "FAILED", "HELM_RELEASE_FAILED")
    path = f"/agents/agent-a/jobs/{job.job_id}/image-pull"
    assert http.get(path, headers=headers()).status_code == 404
    public = queued(db, store, built[1]["public"], revision=2)
    claim(db)
    path = f"/agents/agent-a/jobs/{public.job_id}/image-pull"
    assert http.get(path, headers=headers()).status_code == 404
    assert minter.calls == []


def test_mint_failure_is_a_code_only_503(db, store, built, configured, client):
    http, minter = client
    minter.error = "ECR_TOKEN_UNAVAILABLE"
    job = queued(db, store, built[1]["ecr"])
    claim(db)
    response = http.get(f"/agents/agent-a/jobs/{job.job_id}/image-pull", headers=headers())
    assert (response.status_code, response.json()["detail"]) == (503, "ECR_TOKEN_UNAVAILABLE")


# --- I5–I7 Agent runner (ticket 12 cluster double) ----------------------------------------------

from cmp_providers import agent_image_pull
from cmp_providers.agent_runtime import refresh_pull_secrets
from cmp_providers.onprem_agent import MANAGED, RolloutAgent, RolloutRunner
from cmp_providers.remote_agent import AgentTransportError
from test_onprem_agent import FakeClient, fetch
from test_onprem_agent import FakeCluster as AgentCluster
from test_onprem_agent import job as rollout_job

PULL = {"registry": REGISTRY, "username": "AWS", "password": TOKEN, "expires_at": 2_000_043_200}


def pull_job(**override):
    payload = rollout_job().model_dump(mode="json") | {
        "image_pull": {"secret": "cmp-ecr-pull", "registry": REGISTRY}
    }
    return ProductionRolloutJob.model_validate(payload | override)


class PullCluster(AgentCluster):
    """Adds pod listing (image pull state) and the managed pull Secret listing."""

    def __init__(self):
        super().__init__()
        self.pods = {}  # release -> waiting reason

    def kubectl(self, *args, content=None, timeout=40):
        if args[:2] == ("get", "pods"):
            release = args[args.index("-l") + 1].split("=", 1)[1]
            reason = self.pods.get(release)
            items = (
                [{"status": {"containerStatuses": [{"state": {"waiting": {"reason": reason}}}]}}]
                if reason
                else []
            )
            return CompletedProcess([], 0, json.dumps({"items": items}).encode(), b"")
        return super().kubectl(*args, content=content, timeout=timeout)

    def list_pull_secrets(self):
        return [
            {"metadata": doc["metadata"] | {"namespace": doc["metadata"].get("namespace")}}
            for (kind, _), doc in self.objects.items()
            if kind == "secret"
            and doc
            and (doc.get("metadata") or {}).get("labels", {}).get(agent_image_pull.PURPOSE)
        ]


def pull_secret(cluster):
    secret = cluster.objects[("secret", "cmp-ecr-pull")]
    config = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
    return secret, config["auths"][REGISTRY]


def test_agent_creates_the_pull_secret_and_links_it_before_helm():
    cluster = PullCluster()
    assert RolloutRunner(cluster, fetch, None, lambda: PULL).rollout(pull_job()) == (
        "SUCCEEDED",
        "READY",
    )
    secret, auth = pull_secret(cluster)
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    assert secret["metadata"]["labels"] == {MANAGED: "true", "cmp.dev/purpose": "image-pull"}
    annotations = secret["metadata"]["annotations"]
    assert annotations["cmp.dev/job-id"] == pull_job().job_id
    assert annotations["cmp.dev/pull-expires-at"] == "2000043200"
    assert TOKEN not in json.dumps(annotations)
    assert (auth["username"], auth["password"]) == ("AWS", TOKEN)
    account = cluster.objects[("serviceaccount", "default")]
    assert account["imagePullSecrets"] == [{"name": "cmp-ecr-pull"}]
    helm = cluster.mutations.index(("helm", "demo-db"))
    assert cluster.mutations.index(("apply", "Secret")) < helm
    assert cluster.mutations.index(("apply", "ServiceAccount")) < helm


def test_unmanaged_secret_with_the_pull_name_is_never_overwritten():
    cluster = PullCluster()
    cluster.objects[("secret", "cmp-ecr-pull")] = {"metadata": {}, "data": {"x": "eQ=="}}
    result = RolloutRunner(cluster, fetch, None, lambda: PULL).rollout(pull_job())
    assert result == ("FAILED", "SECRET_NAME_CONFLICT") and cluster.mutations == []


def unreachable():
    raise AgentTransportError("PLATFORM_REQUEST_FAILED")


@pytest.mark.parametrize(
    ("fetcher", "expected"),
    [
        (None, ("FAILED", "IMAGE_PULL_NOT_CONFIGURED")),
        (unreachable, ("UNKNOWN", "IMAGE_PULL_CREDENTIALS_UNAVAILABLE")),
        (lambda: PULL | {"registry": "other"}, ("FAILED", "IMAGE_PULL_CREDENTIALS_INVALID")),
    ],
)
def test_agent_changes_nothing_without_a_valid_pull_credential(fetcher, expected):
    cluster = PullCluster()
    assert RolloutRunner(cluster, fetch, None, fetcher).rollout(pull_job()) == expected
    assert cluster.mutations == []


@pytest.mark.parametrize("reason", ["ErrImagePull", "ImagePullBackOff"])
def test_image_pull_failure_is_reported_as_image_pull_failed(reason):
    cluster = PullCluster()
    cluster.broken.add("demo-app")
    cluster.pods["demo-app"] = reason
    assert RolloutRunner(cluster, fetch, None, lambda: PULL).rollout(pull_job()) == (
        "FAILED",
        "IMAGE_PULL_FAILED",
    )


def test_other_readiness_failures_stay_workload_not_ready():
    cluster = PullCluster()
    cluster.broken.add("demo-app")
    cluster.pods["demo-app"] = "CrashLoopBackOff"
    result = RolloutRunner(cluster, fetch).rollout(rollout_job())
    assert result == ("FAILED", "WORKLOAD_NOT_READY")


def test_job_without_image_pull_keeps_bytes_and_creates_no_pull_secret():
    assert "image_pull" not in rollout_job().model_dump(mode="json")
    cluster = PullCluster()
    assert RolloutRunner(cluster, fetch).rollout(rollout_job())[1] == "READY"
    assert ("secret", "cmp-ecr-pull") not in cluster.objects
    assert ("apply", "ServiceAccount") not in cluster.mutations


class PullClient(FakeClient):
    def __init__(self, payload, responses=None):
        super().__init__(payload)
        self.calls, self.responses = [], list(responses or [])

    def image_pull(self, *, agent_id, credential, job_id):
        self.calls.append((agent_id, job_id))
        response = self.responses.pop(0) if self.responses else PULL
        if isinstance(response, Exception):
            raise response
        return response


def test_daemon_poll_reports_codes_only_and_old_agent_refuses_clearly():
    client = PullClient(pull_job().model_dump(mode="json"))
    cluster = PullCluster()
    RolloutAgent("agent-a", "cred", client, cluster).poll_once()
    [report] = client.reports
    assert (report["status"], report["code"]) == ("SUCCEEDED", "READY")
    assert TOKEN not in json.dumps(report) and client.calls == [("agent-a", pull_job().job_id)]

    # An Agent built before ticket 29 validates the job against a contract without
    # `image_pull` (extra fields forbidden) and reports UNSUPPORTED_JOB instead of hanging.
    from cmp_contracts.provisioning import Contract

    class OldJob(Contract):
        mode: str

    with pytest.raises(ValueError):
        OldJob.model_validate({"mode": "x", "image_pull": {}})


def test_refresher_renews_only_after_half_the_ttl_and_survives_restarts():
    cluster = PullCluster()
    issued = 1_000_000
    pull = PULL | {"expires_at": issued + 43_200}
    RolloutRunner(cluster, fetch, None, lambda: pull).rollout(pull_job())
    annotations = cluster.objects[("secret", "cmp-ecr-pull")]["metadata"]["annotations"]
    annotations["cmp.dev/pull-issued-at"] = str(issued)  # rollout time on the fake clock

    client, logs = PullClient({}, [PULL | {"password": "fresh-SECRET-token"}]), []
    clock = [issued + 21_599]
    refresher = agent_image_pull.PullSecretRefresher(cluster, None, lambda: clock[0])
    refresh_pull_secrets(refresher, client, "agent-a", "cred", logs.append)
    assert client.calls == [] and logs == []  # not yet half the TTL

    clock[0] = issued + 21_600
    # A new refresher (Agent restart) reads the state from the Secret annotations.
    refresher = agent_image_pull.PullSecretRefresher(cluster, None, lambda: clock[0])
    refresh_pull_secrets(refresher, client, "agent-a", "cred", logs.append)
    assert client.calls == [("agent-a", pull_job().job_id)]
    assert pull_secret(cluster)[1]["password"] == "fresh-SECRET-token"
    assert logs == [{"event": "image_pull_refresh", "code": "REFRESHED", "namespace": "demo"}]
    assert "SECRET" not in json.dumps(logs)


def test_refresh_failure_retries_later_and_unauthorized_stops_the_agent():
    cluster = PullCluster()
    RolloutRunner(cluster, fetch, None, lambda: PULL | {"expires_at": 10}).rollout(pull_job())
    cluster.objects[("secret", "cmp-ecr-pull")]["metadata"]["annotations"][
        "cmp.dev/pull-issued-at"
    ] = "0"
    clock = [100]
    client = PullClient({}, [AgentTransportError("PLATFORM_REQUEST_FAILED")])
    refresher, logs = agent_image_pull.PullSecretRefresher(cluster, None, lambda: clock[0]), []
    refresh_pull_secrets(refresher, client, "agent-a", "cred", logs.append)
    assert logs[0]["code"] == "PLATFORM_REQUEST_FAILED"
    refresh_pull_secrets(refresher, client, "agent-a", "cred", logs.append)
    assert len(client.calls) == 1  # backs off instead of calling every poll
    clock[0] += agent_image_pull.RETRY_SECONDS
    client.responses = [AgentTransportError("AGENT_UNAUTHORIZED")]
    with pytest.raises(AgentTransportError):
        refresh_pull_secrets(refresher, client, "agent-a", "cred", logs.append)


# --- I9/I10 GKE: re-execution and Schedule-driven refresh (ticket 11 doubles) --------------------

import test_gcp_workloads as gke
from cmp_api.approvals import PlanExecution
from cmp_worker.activities.gcp_workloads import REFRESH_ACTIVITY
from cmp_worker.workflows import pull_refresh
from temporalio.testing import ActivityEnvironment


async def deployed_gke(db, tmp_path):
    cluster = gke.FakeCluster()
    runtime = gke.FakeRuntime(cluster)
    plan = gke.Plan(tmp_path / "artifacts", db)
    activities = gke.activity_for(db, tmp_path / "artifacts", runtime)
    result = await gke.run(activities, plan.request())
    assert result["code"] == "WORKLOADS_READY"
    with db() as session:
        session.add(
            PlanExecution(
                plan_id="plan-011",
                revision=1,
                step="WORKLOADS",
                approval_id=plan.approval_id,
                status="SUCCEEDED",
                started_at=1,
                finished_at=2,
                code=result["code"],
                resource_refs=result["resource_refs"],
            )
        )
        session.commit()
    return plan, cluster, runtime, activities


def pull_token(cluster):
    data = json.loads(cluster.secrets[(gke.NAMESPACE, "cmp-ecr-pull")][".dockerconfigjson"])
    return data["auths"][gke.REGISTRY]["password"]


async def test_gke_refresh_reapplies_only_the_pull_secret_with_a_new_token(db, tmp_path, caplog):
    caplog.set_level("DEBUG")
    _, cluster, runtime, activities = await deployed_gke(db, tmp_path)
    helm_calls = len(cluster.helm_calls)
    runtime.token_regions.clear()
    cluster.secrets[(gke.NAMESPACE, "cmp-ecr-pull")] = {".dockerconfigjson": "{}"}  # expired
    summary = await ActivityEnvironment().run(activities.refresh_gcp_pull_secrets, {})
    assert summary == {"refreshed": 1, "skipped": 0, "failed": 0, "codes": []}
    assert pull_token(cluster) == gke.ECR_TOKEN and runtime.token_regions == ["ap-northeast-2"]
    assert len(cluster.helm_calls) == helm_calls  # no Helm, no Values change
    assert gke.ECR_TOKEN not in json.dumps(summary) + caplog.text + "".join(cluster.trace)


async def test_gke_refresh_reports_codes_and_ignores_deployments_without_a_pull_secret(
    db, tmp_path
):
    _, _, runtime, activities = await deployed_gke(db, tmp_path)
    runtime.token_error = True
    summary = await ActivityEnvironment().run(activities.refresh_gcp_pull_secrets, {})
    assert summary == {
        "refreshed": 0,
        "skipped": 0,
        "failed": 1,
        "codes": ["IMAGE_PULL_SECRET_FAILED"],
    }
    with db() as session:
        session.get(PlanExecution, ("plan-011", 1, "WORKLOADS")).resource_refs = {}
        session.commit()
    summary = await ActivityEnvironment().run(activities.refresh_gcp_pull_secrets, {})
    assert summary["refreshed"] + summary["failed"] + summary["skipped"] == 0  # public images


async def test_refresh_workflow_calls_the_provisioning_activity(tmp_path):
    from cmp_contracts.queues import PROVISIONING_QUEUE, WORKFLOW_QUEUE
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    assert pull_refresh.REFRESH_ACTIVITY == REFRESH_ACTIVITY
    calls = []

    @activity.defn(name=REFRESH_ACTIVITY)
    async def fake_refresh(request: dict) -> dict:
        calls.append(request)
        return {"refreshed": 1, "skipped": 0, "failed": 0, "codes": []}

    workflows = [pull_refresh.GcpPullSecretRefreshWorkflow]
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue=PROVISIONING_QUEUE, activities=[fake_refresh]),
        Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=workflows),
    ):
        result = await env.client.execute_workflow(
            pull_refresh.GcpPullSecretRefreshWorkflow.run,
            id="refresh-test",
            task_queue=WORKFLOW_QUEUE,
        )
    assert result["refreshed"] == 1 and calls == [{}]


async def test_schedule_is_created_once_every_six_hours():
    from temporalio.client import ScheduleAlreadyRunningError

    class Client:
        def __init__(self):
            self.created = []

        async def create_schedule(self, schedule_id, schedule):
            if self.created:
                raise ScheduleAlreadyRunningError()
            self.created.append((schedule_id, schedule))

    client = Client()
    assert await pull_refresh.ensure_refresh_schedule(client) is True
    assert await pull_refresh.ensure_refresh_schedule(client) is False
    [(schedule_id, schedule)] = client.created
    assert schedule_id == "cmp-gcp-ecr-pull-refresh"
    assert schedule.spec.intervals[0].every.total_seconds() == 6 * 3600


def test_cluster_listing_of_pull_secrets_drops_secret_data(tmp_path):
    from cmp_providers.onprem_agent import ClusterTarget

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("{}")
    target = ClusterTarget(kubeconfig, "ctx", "cluster-a")
    calls = []

    def run(argv, content, timeout):
        calls.append(argv)
        last = "kubectl.kubernetes.io/last-applied-configuration"
        metadata = {
            "name": "cmp-ecr-pull",
            "namespace": "demo",
            "annotations": {last: TOKEN, "cmp.dev/job-id": "rollout-1"},
        }
        body = {"items": [{"metadata": metadata, "data": {"x": TOKEN}}]}
        return CompletedProcess(argv, 0, json.dumps(body).encode(), b"")

    target._exec = run
    [listed] = target.list_pull_secrets()
    assert listed == {
        "metadata": {
            "name": "cmp-ecr-pull",
            "namespace": "demo",
            "labels": {},
            "annotations": {"cmp.dev/job-id": "rollout-1"},
        }
    }
    assert calls[0][-6:] == [
        "secrets",
        "-A",
        "-l",
        "cmp.dev/managed=true,cmp.dev/purpose=image-pull",
        "-o",
        "json",
    ]


async def test_schedule_failure_does_not_stop_worker_startup(monkeypatch):
    import logging

    warnings = []
    logger = logging.getLogger(pull_refresh.SCHEDULE_ID)
    monkeypatch.setattr(logger, "warning", warnings.append)
    assert await pull_refresh.ensure_refresh_schedule(object()) is False
    assert warnings == ["PULL_REFRESH_SCHEDULE_UNAVAILABLE"]
