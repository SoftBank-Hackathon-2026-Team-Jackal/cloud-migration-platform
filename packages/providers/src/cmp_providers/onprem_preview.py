"""ON_PREM K3s Plan Preview from the exact Values bytes a manifest pins (ticket 12A).

Pure function: callers read the bytes from the artifact store. Shows what ticket 12's Agent will
apply (DB StatefulSet + PVC, app Deployment + Service on an existing K3s) without cluster access,
environment values or Secret values. Target/image mismatches become Preview errors so the shared
approval contract refuses the plan.
"""

from collections.abc import Collection, Mapping

from cmp_contracts.chart import PostgresChartValues
from cmp_contracts.plan import PlanManifest, Preview, ResourceChange
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue
from cmp_contracts.shared_deployment_v2 import ApplicationValuesV2
from pydantic import ValidationError

from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.database_url import URL_SECRET_KEY, connection_url_secret_name
from cmp_providers.plans import manifest_digest

VALUE_ROLES = ("database_values", "application_values")


def _reject(code: str, path: str, message: str) -> None:
    raise ProvisioningInputError([ValidationIssue(code=code, path=path, message=message)])


def _load(manifest: PlanManifest, contents: Mapping[str, bytes]):
    for role in VALUE_ROLES:
        if role not in manifest.artifacts or role not in contents:
            _reject("MISSING_ARTIFACT", f"manifest.artifacts.{role}", "Values 산출물이 필요합니다.")
        try:
            verify_artifact_bytes(manifest.artifacts[role], contents[role])
        except ValueError:
            _reject(
                "ARTIFACT_DIGEST_MISMATCH",
                f"manifest.artifacts.{role}",
                "Values 바이트가 계획에 고정된 Digest와 다릅니다.",
            )
    try:
        database = PostgresChartValues.model_validate_json(contents["database_values"])
        application = ApplicationValuesV2.model_validate_json(contents["application_values"])
    except ValidationError:
        _reject("INVALID_VALUES_ARTIFACT", "manifest.artifacts", "Values 형식이 올바르지 않습니다.")
    return database, application


def _resources(cpu: int, memory: int) -> str:
    return f"CPU {cpu}m · 메모리 {memory}MiB"


def onprem_preview(
    manifest: PlanManifest,
    contents: Mapping[str, bytes],
    *,
    platform_secrets: Collection[str] = (),
) -> Preview:
    """`platform_secrets` (ENV-04): Secret names the Agent creates from the approved EnvPlan
    (`<app>-env`, `<db>-conn`); they are not required from the user."""
    target = manifest.target
    if target.provider != "ON_PREM" or manifest.scope not in ("WORKLOADS", "FULL"):
        _reject("UNSUPPORTED_TARGET", "manifest.target", "ON_PREM 워크로드 계획만 지원합니다.")
    database, application = _load(manifest, contents)

    errors: list[ValidationIssue] = []
    for name, values in (("database_values", database), ("application_values", application)):
        if (
            values.environment != "PRODUCTION"
            or values.provider != "ON_PREM"
            or values.namespace != target.namespace
        ):
            errors.append(
                ValidationIssue(
                    code="VALUES_TARGET_MISMATCH",
                    path=f"manifest.artifacts.{name}",
                    message="Values의 환경·Provider·Namespace가 계획 대상과 다릅니다.",
                )
            )
            break
    if manifest.image_uri is not None and application.image.uri != manifest.image_uri:
        errors.append(
            ValidationIssue(
                code="IMAGE_MISMATCH",
                path="manifest.image_uri",
                message="앱 Values의 이미지가 계획에 고정된 이미지와 다릅니다.",
            )
        )

    app = application.application
    secret_sources = [item.source for item in application.env if item.source.kind == "SECRET"]
    db_secret = (database.database.secret_name, database.database.secret_key)
    url_secret = (connection_url_secret_name(database.release_name), URL_SECRET_KEY)
    uses_url = any((source.name, source.key) == url_secret for source in secret_sources)
    user_secrets = sorted(
        {
            f"{source.name}/{source.key}"
            for source in secret_sources
            # DB password and URL Secrets are prepared by the Agent, not the user (ticket 19).
            if (source.name, source.key) not in {db_secret, url_secret}
            and source.name not in platform_secrets
        }
    )
    changes = [
        ResourceChange(
            resource_type="CLUSTER",
            action="NO_CHANGE",
            name=(
                f"기존 K3s {target.cluster_ref.id} · Agent {target.agent_id} · "
                f"Namespace {target.namespace}"
            ),
        ),
        ResourceChange(
            resource_type="DATABASE",
            action="CREATE",
            name=database.release_name,
            quantity=1,
            size=(
                f"PostgreSQL {database.database.version} StatefulSet · "
                f"DB {database.database.name} · "
                f"{_resources(database.resources.cpu_millicores, database.resources.memory_mib)}"
            ),
        ),
        ResourceChange(
            resource_type="STORAGE",
            action="CREATE",
            name=f"{database.release_name} PVC",
            quantity=1,
            size=f"{database.storage.size_gib} GiB · StorageClass {database.storage.class_name}",
        ),
        ResourceChange(
            resource_type="APP",
            action="CREATE",
            name=application.release_name,
            quantity=app.replicas,
            size=(
                f"{application.image.uri} · {_resources(app.cpu_millicores, app.memory_mib)} · "
                f"포트 :{app.port} · 헬스 {app.health_path} · "
                f"환경변수 {len(application.env)}개(Secret 참조 {len(secret_sources)}개)"
            ),
        ),
        ResourceChange(
            resource_type="APP",
            action="CREATE",
            name=f"{application.release_name} Service",
            quantity=1,
            size=f"클러스터 내부 :{app.port}",
        ),
        ResourceChange(
            resource_type="INGRESS",
            action="NO_CHANGE",
            name="외부 공개 없음; 클러스터 내부 Service만",
        ),
    ]
    defaults = [
        ResourceChange(
            resource_type="CLUSTER",
            action="CREATE",
            name=f"Namespace {target.namespace}가 없으면 플랫폼 관리 라벨로 생성",
        ),
        ResourceChange(
            resource_type="DATABASE",
            action="CREATE",
            name=(
                f"DB 비밀번호 Secret {db_secret[0]}/{db_secret[1]}가 없으면 "
                "클러스터 안에서 생성(값은 외부로 나가지 않음)"
            ),
        ),
    ]
    if uses_url:
        defaults.append(
            ResourceChange(
                resource_type="DATABASE",
                action="CREATE",
                name=(
                    f"DB 연결 URL Secret {url_secret[0]}/{url_secret[1]}를 비밀번호로 "
                    "클러스터 안에서 생성·갱신(값은 외부로 나가지 않음)"
                ),
            )
        )
    warnings = [
        ValidationIssue(
            code="CLUSTER_STATE_NOT_INSPECTED",
            path="target",
            message=(
                "미리보기는 클러스터를 조회하지 않습니다. 같은 이름의 Release가 있으면 "
                "실행 시 소유권을 확인한 뒤 갱신합니다."
            ),
        )
    ]
    if user_secrets:
        warnings.append(
            ValidationIssue(
                code="USER_SECRET_REQUIRED",
                path="application_values.env",
                message="실행 전 대상 Namespace에 있어야 하는 Secret: " + ", ".join(user_secrets),
            )
        )
    return Preview(
        contract_version="1",
        manifest_digest=manifest_digest(manifest),
        changes=changes,
        applied_defaults=defaults,
        warnings=warnings,
        errors=errors,
    )
