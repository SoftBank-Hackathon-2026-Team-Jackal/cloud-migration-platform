"""Build and project ProvisioningResult v1 (ticket 13, decisions R1~R8). Pure functions only.

`build_provisioning_result` turns one finished approved execution (status, code, failed step and
the provider refs recorded by tickets 08~12) into the common contract. Only known keys are read,
so anything else a provider might return is dropped. Each provider runs its checks in a fixed
order and stops at the first failure, so the check list is inferred from that order (R5).
"""

from datetime import datetime

from cmp_contracts.plan import PlanManifest
from cmp_contracts.provisioning_result import (
    ApplicationHandoff,
    Check,
    ClusterRef,
    DatabaseHandoff,
    DatabaseStorage,
    Failure,
    IngressRef,
    ProvisioningResult,
    SecretLocation,
)

DEPLOY, VERIFY = "WORKLOAD_DEPLOY", "POST_DEPLOY_VERIFICATION"

# (layer, name, stage when it fails, failure codes) in execution order.
INFRA_CHECKS = [
    (
        "INFRASTRUCTURE",
        "terraform_apply",
        "INFRASTRUCTURE",
        {"TERRAFORM_INIT_FAILED", "TERRAFORM_APPLY_FAILED", "TERRAFORM_APPLY_INTERRUPTED"},
    ),
    ("INFRASTRUCTURE", "cluster_ready", "INFRASTRUCTURE", {"CLUSTER_NOT_READY"}),
]
CLUSTER_CODES = {
    "CLUSTER_ACCESS_FAILED",
    "CLUSTER_NOT_RUNNING",
    "CLUSTER_TARGET_UNAVAILABLE",
    "GCP_IDENTITY_UNAVAILABLE",
    "AWS_IDENTITY_UNAVAILABLE",
    "PROJECT_MISMATCH",
    "ACCOUNT_MISMATCH",
    "REGION_MISMATCH",
    "ECR_ACCOUNT_MISMATCH",
}
HELM_CODES = {"HELM_RELEASE_PENDING", "HELM_RELEASE_FAILED", "HELM_RESULT_UNKNOWN"}
POD_CODES = {"IMAGE_PULL_FAILED", "SECRET_REFERENCE_INVALID", "POD_CRASHING", "POD_NOT_READY"}
CLOUD_WORKLOAD_CHECKS = [  # tickets 09/11 shared flow (k8s_workloads._deploy)
    ("INFRASTRUCTURE", "cluster_access", DEPLOY, CLUSTER_CODES),
    ("INFRASTRUCTURE", "ingress_controller", DEPLOY, {"LB_CONTROLLER_NOT_READY"}),
    ("INFRASTRUCTURE", "storage_class", DEPLOY, {"STORAGE_CLASS_MISMATCH"}),
    ("WORKLOAD", "image_pull_secret", DEPLOY, {"IMAGE_PULL_SECRET_FAILED"}),
    (
        "WORKLOAD",
        "secret_refs",
        DEPLOY,
        {"USER_SECRET_MISSING", "CONFIG_MAP_MISSING", "DB_SECRET_INVALID"},
    ),
    ("WORKLOAD", "db_release", DEPLOY, HELM_CODES),
    ("INFRASTRUCTURE", "pvc_bound", VERIFY, {"PVC_NOT_BOUND"}),
    ("WORKLOAD", "db_pod_ready", VERIFY, POD_CODES),
    ("WORKLOAD", "db_auth_connect", VERIFY, {"DB_CONNECT_FAILED", "DB_VERSION_MISMATCH"}),
    ("WORKLOAD", "app_release", DEPLOY, HELM_CODES),
    ("WORKLOAD", "app_pod_ready", VERIFY, POD_CODES),
    ("APPLICATION", "internal_health", VERIFY, {"HEALTH_CHECK_FAILED"}),
    ("APPLICATION", "external_address", VERIFY, {"ALB_ADDRESS_PENDING", "INGRESS_ADDRESS_PENDING"}),
    ("APPLICATION", "external_health", VERIFY, {"ALB_UNREACHABLE", "INGRESS_UNREACHABLE"}),
]
ONPREM_CHECKS = [  # ticket 12 Agent rollout order (onprem_agent.RolloutRunner)
    (
        "INFRASTRUCTURE",
        "cluster_access",
        DEPLOY,
        {"KUBERNETES_UNAVAILABLE", "CLUSTER_MISMATCH", "NAMESPACE_NOT_OWNED"},
    ),
    ("INFRASTRUCTURE", "storage_class", DEPLOY, {"STORAGE_CLASS_MISSING"}),
    ("WORKLOAD", "secret_refs", DEPLOY, {"SECRET_REFERENCE_MISSING"}),
    (
        "WORKLOAD",
        "helm_releases",
        DEPLOY,
        {"HELM_RELEASE_FAILED", "RELEASE_NOT_DEPLOYED", "KUBERNETES_OPERATION_FAILED"},
    ),
    ("WORKLOAD", "pods_ready", VERIFY, {"WORKLOAD_MISSING", "WORKLOAD_NOT_READY"}),
    ("WORKLOAD", "service_endpoints", VERIFY, {"SERVICE_ENDPOINT_NOT_READY"}),
    ("INFRASTRUCTURE", "pvc_bound", VERIFY, {"PVC_NOT_BOUND"}),
    ("WORKLOAD", "db_ready", VERIFY, {"DATABASE_NOT_READY"}),
    ("APPLICATION", "internal_health", VERIFY, {"APPLICATION_HEALTH_FAILED"}),
]
# Checks the execution itself never performs; recorded so consumers see them as not done.
NOT_PERFORMED = {
    "CLOUD": [("APPLICATION", "functional_tests", "AFTER_DATA_MIGRATION")],
    "ON_PREM": [
        ("APPLICATION", "external_health", "NO_EXTERNAL_INGRESS"),
        ("APPLICATION", "functional_tests", "AFTER_DATA_MIGRATION"),
    ],
}
# Failures before any target change: approval/Revision/Digest/artifact/Values gates.
PRECONDITION_CODES = {
    "APPROVAL_NOT_FOUND",
    "PLAN_NOT_FOUND",
    "APPROVAL_TARGET_MISMATCH",
    "PLAN_NOT_APPROVED",
    "STALE_REVISION",
    "MANIFEST_DIGEST_MISMATCH",
    "PROVIDER_MISMATCH",
    "SCOPE_MISMATCH",
    "NO_EXECUTION_PATH",
    "UNSUPPORTED_EXECUTION_SCOPE",
    "ARTIFACT_ROOT_UNCONFIGURED",
    "ARTIFACT_MISSING",
    "ARTIFACT_NOT_FOUND",
    "MISSING_ARTIFACT",
    "ARTIFACT_DIGEST_MISMATCH",
    "ARTIFACT_UNAVAILABLE",
    "INVALID_VALUES",
    "INVALID_APP_VALUES",
    "INVALID_VALUES_ARTIFACT",
    "VALUES_TARGET_MISMATCH",
    "BINDINGS_MISMATCH",
    "NAMESPACE_MISMATCH",
    "APP_IMAGE_MISMATCH",
    "DB_BINDING_MISMATCH",
    "TARGET_MISMATCH",
    "STATE_MISMATCH",
    "TEMPLATE_DRIFT",
    "SAVED_PLAN_STALE",
    "LOCAL_STATE_PLAN",
    "MISSING_PROFILE",
    "MISSING_ADC_PRINCIPAL",
    "ADC_PRINCIPAL_MISMATCH",
    "STATE_ACCOUNT_MISMATCH",
    "STATE_ACCOUNT_CHECK_FAILED",
    "ACCOUNT_CHECK_FAILED",
    "JOB_TARGET_MISMATCH",
    "EXPLICIT_TARGET_REQUIRED",
    "UNSUPPORTED_JOB",
}
NETWORK_REF_KEYS = ("vpc_id", "vpc_network", "subnetwork")
CLUSTER_KIND = {"AWS": "EKS", "GCP": "GKE", "ON_PREM": "K3s"}


def _int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bool(value) -> bool | None:
    return {"true": True, "false": False}.get(str(value).lower()) if value is not None else None


def _secret(namespace: str | None, ref: str | None) -> SecretLocation | None:
    name, _, key = (ref or "").partition("/")
    if not (namespace and name and key):
        return None
    return SecretLocation(namespace=namespace, name=name, key=key)


def _checks(provider: str, scope: str, status: str, code: str, step: str | None, refs: dict):
    """R5/R6: ordered checks -> statuses, plus the failure stage of the failed one."""
    infra = scope in ("INFRASTRUCTURE", "FULL") and provider != "ON_PREM"
    workloads = scope in ("WORKLOADS", "FULL")
    if provider == "ON_PREM":
        workload_checks = ONPREM_CHECKS
    else:
        # Only AWS installs an ingress controller; only GCP prepares an ECR pull Secret.
        skip = {"GCP": {"ingress_controller"}, "AWS": {"image_pull_secret"}}[provider]
        workload_checks = [c for c in CLOUD_WORKLOAD_CHECKS if c[1] not in skip]
    catalogue = (INFRA_CHECKS if infra else []) + (workload_checks if workloads else [])
    extra = NOT_PERFORMED["ON_PREM" if provider == "ON_PREM" else "CLOUD"] if workloads else []

    if status == "SUCCEEDED":
        failed_at, stage = len(catalogue), None
    else:
        infra_step = bool(step and step.endswith("_plan") and provider != "ON_PREM")
        start = 0 if infra_step or not infra else len(INFRA_CHECKS)
        failed_at = None
        for index in range(start, len(catalogue)):
            _, name, check_stage, codes = catalogue[index]
            if code not in codes:
                continue
            # The same Helm/Pod codes guard both releases; the DB one has passed once its ref
            # is recorded (W8/W9 record refs right after each step succeeds).
            if code in HELM_CODES and name == "db_release" and "db_release" in refs:
                continue
            if code in POD_CODES and name == "db_pod_ready" and "app_release" in refs:
                continue
            failed_at, stage = index, check_stage
            break
        if failed_at is None:
            if code in PRECONDITION_CODES:
                stage = "PRECONDITION"
            elif infra_step:
                stage = "INFRASTRUCTURE"
            else:
                stage = DEPLOY
            # Unmapped: what ran is unknown, so nothing is claimed as passed.
            fill = "UNKNOWN" if status == "UNKNOWN" and stage != "PRECONDITION" else "NOT_RUN"
            if stage == "INFRASTRUCTURE" or not infra:
                passed = 0
            else:
                passed = len(INFRA_CHECKS)  # infra step succeeded before the workload step
            checks = [
                Check(layer=layer, name=name, status="PASSED" if i < passed else fill)
                for i, (layer, name, _, _) in enumerate(catalogue)
            ]
            return checks + _extra(extra), stage

    checks = []
    for index, (layer, name, _, _) in enumerate(catalogue):
        if index < failed_at:
            checks.append(Check(layer=layer, name=name, status="PASSED"))
        elif index == failed_at:
            mark = "UNKNOWN" if status == "UNKNOWN" else "FAILED"
            checks.append(Check(layer=layer, name=name, status=mark, code=code))
        else:
            checks.append(Check(layer=layer, name=name, status="NOT_RUN"))
    return checks + _extra(extra), stage


def _extra(items) -> list[Check]:
    return [
        Check(layer=layer, name=name, status="NOT_RUN", reason=reason)
        for layer, name, reason in items
    ]


def _database(provider, namespace, refs, ready) -> DatabaseHandoff:
    return DatabaseHandoff(
        version=refs.get("db_version"),
        host=refs.get("db_host"),
        port=_int(refs.get("db_port")),
        name=refs.get("db_name"),
        user=refs.get("db_user"),
        password_secret=_secret(namespace, refs.get("db_secret_ref")),
        replication_capable=_bool(refs.get("db_replication_capable")),
        storage=DatabaseStorage(
            pvc=refs.get("db_pvc") or (refs.get("db_release") if ready else None),
            storage_class=refs.get("storage_class"),
            size_gib=_int(refs.get("db_storage_size_gib")),
        ),
        release=refs.get("db_release"),
        ready=ready and bool(refs.get("db_host")),
    )


def _application(provider, refs, ready) -> ApplicationHandoff:
    ingress = None
    if provider == "AWS" and (refs.get("ingress") or refs.get("alb_hostname")):
        ingress = IngressRef(kind="ALB", name=refs.get("ingress"), address=refs.get("alb_hostname"))
    elif provider == "GCP" and (refs.get("ingress") or refs.get("ingress_ip")):
        ingress = IngressRef(
            kind="GKE_INGRESS", name=refs.get("ingress"), address=refs.get("ingress_ip")
        )
    return ApplicationHandoff(
        release=refs.get("app_release"),
        image=refs.get("app_image"),
        internal_endpoint=refs.get("app_internal_endpoint"),
        external_endpoint=refs.get("app_external_endpoint"),
        ingress=ingress,
        port=_int(refs.get("app_port")),
        health_path=refs.get("app_health_path"),
        replicas=_int(refs.get("app_replicas")),
        ready=ready and bool(refs.get("app_internal_endpoint")),
    )


def build_provisioning_result(
    manifest: PlanManifest,
    *,
    approval_id: str,
    status: str,
    code: str,
    step: str | None,
    refs: dict[str, str],
    finished_at: datetime,
) -> ProvisioningResult:
    target = manifest.target
    provider, scope = target.provider, manifest.scope
    # Blank or padded values are treated as absent so one odd ref cannot reject the result.
    refs = {k: v.strip() for k, v in refs.items() if isinstance(v, str) and v.strip()}
    workloads = scope in ("WORKLOADS", "FULL")
    succeeded = status == "SUCCEEDED"
    if succeeded:
        result_status = "READY" if workloads else "INFRASTRUCTURE_READY"
    else:
        result_status = status if status in ("FAILED", "UNKNOWN") else "UNKNOWN"

    if provider == "ON_PREM":
        cluster = ClusterRef(
            kind="K3s",
            name=None,
            agent_id=refs.get("agent_id") or target.agent_id,
            cluster_uid=refs.get("cluster_uid"),
        )
        namespace = refs.get("namespace") or target.namespace
    else:
        name_key = "eks_cluster_name" if provider == "AWS" else "gke_cluster_name"
        cluster = ClusterRef(
            kind=CLUSTER_KIND[provider],
            name=refs.get(name_key) or refs.get("cluster_name"),
            location=refs.get("gke_location") or target.region,
            account_id=refs.get("gcp_project_id") or target.account_id,
            network_refs={k: refs[k] for k in NETWORK_REF_KEYS if refs.get(k)},
        )
        namespace = refs.get("namespace")

    checks, stage = _checks(provider, scope, status, code, step, refs)
    failure = None
    if not succeeded:
        recover = "CHECK_STATE_FIRST" if status == "UNKNOWN" else "RETRY_AFTER_FIX"
        failure = Failure(stage=stage, code=code, recoverability=recover)

    # Infrastructure-only results, and failures before any workload ref exists, carry no
    # database/application section rather than an empty one.
    started_workloads = workloads and (succeeded or step is None or not step.endswith("_plan"))
    if provider == "ON_PREM":
        started_workloads = workloads
    database = application = None
    if started_workloads and (succeeded or stage != "PRECONDITION"):
        database = _database(provider, namespace, refs, succeeded)
        application = _application(provider, refs, succeeded)

    warnings = []
    if database and succeeded and database.replication_capable is None:
        warnings.append("REPLICATION_NOT_VERIFIED")
    if application and succeeded and application.external_endpoint is None:
        warnings.append("EXTERNAL_ENDPOINT_UNAVAILABLE")

    return ProvisioningResult(
        migration_id=manifest.migration_id,
        plan_id=manifest.plan.plan_id,
        revision=manifest.plan.revision,
        approval_id=approval_id,
        provider=provider,
        scope=scope,
        status=result_status,
        code=code,
        cluster=cluster,
        namespace=namespace,
        finished_at=finished_at,
        database=database,
        application=application,
        verification=checks,
        failure=failure,
        warnings=warnings,
    )


def handoff(result: ProvisioningResult | None, consumer: str, plan_id: str, revision: int):
    """R7: the view one downstream Worker needs, with a proceed/blocked decision."""
    base = {"plan_id": plan_id, "revision": revision, "consumer": consumer}
    if result is None:
        return {**base, "proceed": False, "blocked_by": ["RESULT_NOT_FOUND"]}
    blocked = []
    if result.status == "INFRASTRUCTURE_READY":
        blocked.append("WORKLOADS_NOT_DEPLOYED")
    elif result.status != "READY":
        blocked.append("PROVISIONING_NOT_READY")
    common = result.model_dump(
        mode="json", include={"migration_id", "plan_id", "revision", "provider", "status", "code",
                              "data_state", "cluster", "namespace", "finished_at", "warnings"}
    )  # fmt: skip
    view = {**base, "common": common}
    if consumer == "DATA_MIGRATION":
        if result.status == "READY" and not (result.database and result.database.ready):
            blocked.append("DATABASE_NOT_READY")
        view["database"] = result.database.model_dump(mode="json") if result.database else None
    else:
        app = result.application
        if result.status == "READY":
            if not (app and app.ready):
                blocked.append("APPLICATION_NOT_READY")
            elif not app.external_endpoint:
                blocked.append("EXTERNAL_ENDPOINT_UNAVAILABLE")
        view["application"] = app.model_dump(mode="json") if app else None
    return {**view, "proceed": not blocked, "blocked_by": blocked}


def public_summary(result: ProvisioningResult) -> dict:
    """R8: the user-facing summary. No DB section, internal endpoints, releases or IDs."""
    app = result.application
    return {
        "plan_id": result.plan_id,
        "revision": result.revision,
        "provider": result.provider,
        "scope": result.scope,
        "status": result.status,
        "code": result.code,
        "data_state": result.data_state,
        "cluster": {
            "kind": result.cluster.kind,
            "name": result.cluster.name,
            "location": result.cluster.location,
        },
        "application": None
        if app is None
        else {
            "external_endpoint": app.external_endpoint,
            "health_path": app.health_path,
            "scheme": app.scheme,
        },
        "verification": [check.model_dump(mode="json") for check in result.verification],
        "failure": result.failure.model_dump(mode="json") if result.failure else None,
        "warnings": list(result.warnings),
        "finished_at": result.finished_at.isoformat(),
    }
