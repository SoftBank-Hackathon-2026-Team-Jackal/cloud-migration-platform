"""Preview of an AWS/GCP WORKLOADS Plan for the 09/11 DB + App deployment (ticket 22, W7).

Pure function: reads only the approved Values bytes the manifest pins, never a cluster. Shows
the existing cluster (from the infrastructure run), DB StatefulSet/PVC, App Deployment, the
provider Ingress and the defaults 09/11 apply. Secret values never appear, only names/keys.
"""

import json

from cmp_contracts.chart import PostgresChartValues
from cmp_contracts.plan import PlanManifest, Preview, ResourceChange
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue

from cmp_providers.artifacts import verify_artifact_bytes
from cmp_providers.plans import manifest_digest

INGRESS = {"AWS": "AWS ALB(internet-facing, HTTP:80)", "GCP": "GCE Ingress(외부 HTTP:80)"}
PROVISIONER = {"AWS": "ebs.csi.aws.com(gp3)", "GCP": "pd.csi.storage.gke.io(pd-balanced)"}


def _reject(code: str, path: str) -> None:
    raise ProvisioningInputError(
        [ValidationIssue(code=code, path=path, message="Invalid workload Plan input.")]
    )


def cloud_workload_preview(
    manifest: PlanManifest, contents: dict[str, bytes], *, cluster_name: str, deployed: bool
) -> Preview:
    """`deployed`: an earlier Revision of this WORKLOADS Plan already ran successfully."""
    provider = manifest.target.provider
    if provider not in INGRESS or manifest.scope != "WORKLOADS":
        _reject("UNSUPPORTED_TARGET", "manifest.target")
    for role in ("database_values", "application_values"):
        ref = manifest.artifacts.get(role)
        if ref is None or role not in contents:
            _reject("MISSING_ARTIFACT", f"manifest.artifacts.{role}")
        try:
            verify_artifact_bytes(ref, contents[role])
        except ValueError:
            _reject("ARTIFACT_DIGEST_MISMATCH", f"manifest.artifacts.{role}")
    db = PostgresChartValues.model_validate_json(contents["database_values"])
    app = json.loads(contents["application_values"])
    errors = []
    if (db.provider, app["provider"]) != (provider, provider) or app["namespace"] != db.namespace:
        errors.append(
            ValidationIssue(
                code="VALUES_TARGET_MISMATCH",
                path="application_values",
                message="Values describe a different target.",
            )
        )
    if app["image"]["uri"] != manifest.image_uri:
        errors.append(
            ValidationIssue(
                code="IMAGE_MISMATCH",
                path="application_values.image",
                message="Values image differs from the Plan image.",
            )
        )
    action = "UPDATE" if deployed else "CREATE"
    settings = app["application"]
    name, namespace = app["release_name"], db.namespace
    changes = [
        ResourceChange(
            resource_type="CLUSTER",
            action="NO_CHANGE",
            name=f"인프라 실행이 만든 클러스터 {cluster_name} · Namespace {namespace}",
        ),
        ResourceChange(
            resource_type="STORAGE",
            action="READ",
            name=f"StorageClass {db.storage.class_name}",
            size=f"없으면 {PROVISIONER[provider]}로 생성, 다른 Provisioner면 실행 거절",
        ),
        ResourceChange(
            resource_type="DATABASE",
            action=action,
            name=db.release_name,
            quantity=1,
            size=(
                f"PostgreSQL {db.database.version} StatefulSet · DB {db.database.name} · "
                f"PVC {db.storage.size_gib} GiB"
            ),
        ),
        ResourceChange(
            resource_type="APP",
            action=action,
            name=name,
            quantity=settings["replicas"],
            size=(
                f"{app['image']['uri']} · CPU {settings['cpu_millicores']}m · "
                f"메모리 {settings['memory_mib']}MiB · 포트 :{settings['port']} · "
                f"헬스 {settings['health_path']} · 환경변수 {len(app['env'])}개"
            ),
        ),
        ResourceChange(resource_type="INGRESS", action=action, name=name, size=INGRESS[provider]),
    ]
    defaults = [
        ResourceChange(
            resource_type="CLUSTER",
            action="CREATE",
            name=f"Namespace {namespace}가 없으면 플랫폼 관리 라벨로 생성",
        ),
        ResourceChange(
            resource_type="DATABASE",
            action="CREATE",
            name=(
                f"DB 비밀번호 Secret {db.database.secret_name}/{db.database.secret_key}가 없으면 "
                "클러스터 안에서 생성(값은 외부로 나가지 않음)"
            ),
        ),
    ]
    warnings = [
        ValidationIssue(
            code="CLUSTER_STATE_NOT_INSPECTED",
            path="target",
            message=(
                "미리보기는 클러스터를 조회하지 않습니다. 같은 이름의 Release가 있으면 "
                "실행 시 상태를 확인한 뒤 갱신합니다."
            ),
        )
    ]
    return Preview(
        contract_version="1",
        manifest_digest=manifest_digest(manifest),
        changes=changes,
        applied_defaults=defaults,
        warnings=warnings,
        errors=errors,
    )
