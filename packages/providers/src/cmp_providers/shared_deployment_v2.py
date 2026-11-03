"""Render resource-ID based DB/app Charts; callers own Secret and cluster lifecycles."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from cmp_contracts.chart import DatabaseSettings, PersistentStorage, PostgresChartValues
from cmp_contracts.provisioning import Application, ProvisioningInputError, ValidationIssue
from cmp_contracts.provisioning_v2 import ResourceOutputBinding
from cmp_contracts.shared_deployment import SecretReference
from cmp_contracts.shared_deployment_v2 import (
    ApplicationResourceV2,
    ApplicationValuesV2,
    DatabaseOutputsV2,
    DatabaseResourceV2,
    LiteralEnvSourceV2,
    RenderedApplicationV2,
    RenderedDatabaseV2,
    ResolvedEnvV2,
    SecretEnvSourceV2,
    SharedDeploymentRequestV2,
    SharedDeploymentResultV2,
)
from pydantic import ValidationError

from cmp_providers.artifacts import canonical_json_bytes


def _issue(code: str, path: str) -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message="Invalid shared deployment input.")


def _invalid(code: str, path: str = "shared_deployment_v2") -> ProvisioningInputError:
    return ProvisioningInputError([_issue(code, path)])


def _render(values, chart_dir: Path, helm_bin: str) -> str:
    """Helm template is external I/O; never expose its stdout/stderr on failure."""
    try:
        with TemporaryDirectory(prefix="cmp-render-v2-") as directory:
            path = Path(directory) / "values.json"
            path.write_bytes(canonical_json_bytes(values.model_dump(mode="json")))
            result = subprocess.run(
                [
                    helm_bin,
                    "template",
                    values.release_name,
                    str(chart_dir.resolve()),
                    "--namespace",
                    values.namespace,
                    "--values",
                    str(path),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if result.returncode:
                raise _invalid("DEPLOYMENT_RENDER_FAILED")
            return result.stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise _invalid("DEPLOYMENT_RENDER_FAILED") from None


def render_shared_deployment_v2(
    payload: dict,
    *,
    database_chart_dir: Path,
    application_chart_dir: Path,
    helm_bin: str = "helm",
) -> SharedDeploymentResultV2:
    """Return final Values/Manifest and resource outputs; never Apply or inspect a cluster."""
    try:
        request = SharedDeploymentRequestV2.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise _invalid("INVALID_DEPLOYMENT_INPUT") from None

    issues: list[ValidationIssue] = []
    resources = {}
    releases = set()
    databases = {}
    applications = []
    for index, resource in enumerate(request.resources):
        if resource.id in resources:
            issues.append(_issue("DUPLICATE_RESOURCE_ID", f"resources.{index}.id"))
        resources[resource.id] = resource
        if resource.release_name in releases:
            issues.append(_issue("DUPLICATE_RELEASE", f"resources.{index}.release_name"))
        releases.add(resource.release_name)
        if isinstance(resource, DatabaseResourceV2):
            databases[resource.id] = resource
            if resource.extensions:
                issues.append(_issue("UNSUPPORTED_DB_EXTENSION", f"resources.{index}.extensions"))
        else:
            applications.append(resource)
    if not databases:
        issues.append(_issue("MISSING_RESOURCE_KIND", "resources"))
    if not applications:
        issues.append(_issue("MISSING_RESOURCE_KIND", "resources"))
    for index, resource in enumerate(request.resources):
        if not isinstance(resource, ApplicationResourceV2):
            continue
        for binding in resource.env_bindings.values():
            if not isinstance(binding, ResourceOutputBinding):
                continue
            if binding.resource_id not in resources:
                issues.append(
                    _issue("UNKNOWN_RESOURCE_REF", f"resources.{index}.env_bindings.<env>")
                )
            elif binding.resource_id not in databases:
                issues.append(
                    _issue("UNSUPPORTED_RESOURCE_OUTPUT", f"resources.{index}.env_bindings.<env>")
                )
    if issues:
        raise ProvisioningInputError(issues)

    rendered = {}
    outputs = {}
    for resource in request.resources:
        if not isinstance(resource, DatabaseResourceV2):
            continue
        db = resource.database
        values = PostgresChartValues(
            contract_version="1",  # The v2 resource graph reuses the existing DB Chart Values.
            environment=request.environment,
            provider=request.provider,
            release_name=resource.release_name,
            namespace=request.namespace,
            image=resource.image,
            database=DatabaseSettings(
                version=resource.version,
                name=db.name,
                username=db.username,
                secret_name=db.password_secret_ref.name,
                secret_key=db.password_secret_ref.key,
            ),
            resources=resource.resources,
            storage=PersistentStorage(
                class_name=resource.storage.class_name, size_gib=resource.storage.size_gib
            ),
        )
        service_host = f"{resource.release_name}.{request.namespace}.svc"
        output = DatabaseOutputsV2(
            host=service_host,
            port=5432,
            name=db.name,
            username=db.username,
            password_secret_ref=db.password_secret_ref,
        )
        outputs[resource.id] = output
        rendered[resource.id] = RenderedDatabaseV2(
            id=resource.id,
            release_name=resource.release_name,
            helm_values=values,
            manifest_yaml=_render(values, database_chart_dir, helm_bin),
            outputs=output,
        )

    for resource in applications:
        env = []
        resolved = {}
        for name, binding in resource.env_bindings.items():
            if isinstance(binding, ResourceOutputBinding):
                database = outputs[binding.resource_id]
                value = getattr(database, binding.output)
                if binding.output == "password_secret_ref":
                    resolved_value = SecretReference(secret_name=value.name, secret_key=value.key)
                else:
                    resolved_value = str(value)
            else:
                resolved_value = SecretReference(
                    secret_name=binding.secret_ref.name, secret_key=binding.secret_ref.key
                )
            resolved[name] = resolved_value
            source = (
                SecretEnvSourceV2(
                    kind="SECRET", name=resolved_value.secret_name, key=resolved_value.secret_key
                )
                if isinstance(resolved_value, SecretReference)
                else LiteralEnvSourceV2(kind="LITERAL", value=resolved_value)
            )
            env.append(ResolvedEnvV2(name=name, source=source))
        values = ApplicationValuesV2(
            contract_version="2",
            environment=request.environment,
            provider=request.provider,
            release_name=resource.release_name,
            namespace=request.namespace,
            image=resource.image,
            config_ref=resource.config_ref,
            application=Application(
                replicas=resource.replicas,
                cpu_millicores=resource.resources.cpu_millicores,
                memory_mib=resource.resources.memory_mib,
                port=resource.port,
                health_path=resource.health_path,
            ),
            required_env=list(resource.env_bindings),
            env=env,
        )
        rendered[resource.id] = RenderedApplicationV2(
            id=resource.id,
            release_name=resource.release_name,
            helm_values=values,
            manifest_yaml=_render(values, application_chart_dir, helm_bin),
            resolved_env_bindings=resolved,
        )
    return SharedDeploymentResultV2(
        environment=request.environment,
        provider=request.provider,
        namespace=request.namespace,
        resources=[rendered[resource.id] for resource in request.resources],
    )
