"""Render shared DB/app workloads without touching a Kubernetes cluster."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from cmp_contracts.application import EnvironmentBinding
from cmp_contracts.chart import (
    DatabaseSettings,
    PersistentStorage,
    PodResources,
    PostgresChartValues,
)
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue
from cmp_contracts.shared_deployment import (
    DatabaseService,
    RenderedApplication,
    RenderedDatabase,
    SecretReference,
    SharedDeploymentRequest,
    SharedDeploymentResult,
)
from pydantic import ValidationError

from cmp_providers.app_renderer import ApplicationRenderError, render_application
from cmp_providers.application import resolve_application
from cmp_providers.artifacts import canonical_json_bytes


def _invalid(code: str) -> ProvisioningInputError:
    return ProvisioningInputError(
        [ValidationIssue(code=code, path="shared_deployment", message="Invalid shared deployment")]
    )


def _render_database(values: PostgresChartValues, chart_dir: Path, helm_bin: str) -> str:
    try:
        with TemporaryDirectory(prefix="cmp-db-render-") as directory:
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
                raise _invalid("DATABASE_RENDER_FAILED")
            return result.stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise _invalid("DATABASE_RENDER_FAILED") from None


def render_shared_deployment(
    payload: dict,
    *,
    database_chart_dir: Path,
    application_chart_dir: Path,
    helm_bin: str = "helm",
) -> SharedDeploymentResult:
    """Return exact render output and references; Worker owns storage, Apply and verification."""
    try:
        request = SharedDeploymentRequest.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise _invalid("INVALID_DEPLOYMENT_INPUT") from None

    db = request.db
    database_values = PostgresChartValues(
        contract_version=request.contract_version,
        environment=request.environment,
        provider=request.provider,
        release_name=request.release_names.db,
        namespace=request.namespace,
        image=db.image,
        database=DatabaseSettings(
            version=db.version,
            name=db.name,
            username=db.username,
            secret_name=db.password_secret_name,
            secret_key=db.password_secret_key,
        ),
        resources=PodResources(cpu_millicores=db.cpu_millicores, memory_mib=db.memory_mib),
        storage=PersistentStorage(class_name=request.storage_class, size_gib=db.pvc_size_gib),
    )
    service = DatabaseService(host=f"{request.release_names.db}.{request.namespace}.svc", port=5432)
    app = request.app
    env = [
        EnvironmentBinding(
            name=name,
            source=(
                {"kind": "DATABASE", "field": source.removeprefix("database.")}
                if isinstance(source, str)
                else source.model_dump(mode="json")
            ),
        )
        for name, source in request.env_bindings.items()
    ]
    application_template = {
        "contract_version": request.contract_version,
        "environment": request.environment,
        "provider": request.provider,
        "release_name": request.release_names.app,
        "namespace": request.namespace,
        "config_ref": app.config_ref.model_dump(mode="json"),
        "image": app.image.model_dump(mode="json"),
        "application": app.application().model_dump(mode="json"),
        "database": {
            "host": None,
            "port": 5432,
            "name": db.name,
            "username": db.username,
            "secret_name": None,
            "secret_key": db.password_secret_key,
        },
        "required_env": [item.name for item in env],
        "env": [item.model_dump(mode="json") for item in env],
    }
    dynamic = {item.source.field for item in env if item.source.kind == "DATABASE"}
    resolved = {}
    if "host" in dynamic:
        resolved["DATABASE_HOST"] = service.host
    if "secret_ref" in dynamic:
        resolved["DATABASE_SECRET_NAME"] = db.password_secret_name
    try:
        application_values_dict = resolve_application(application_template, resolved)
        database_manifest = _render_database(database_values, database_chart_dir, helm_bin)
        application_manifest = render_application(
            application_values_dict, chart_dir=application_chart_dir, helm_bin=helm_bin
        ).decode("utf-8")
    except (ProvisioningInputError, ApplicationRenderError, UnicodeError):
        raise _invalid("DEPLOYMENT_RENDER_FAILED") from None

    database_fields = {
        "host": service.host,
        "port": "5432",
        "name": db.name,
        "username": db.username,
        "secret_ref": SecretReference(
            secret_name=db.password_secret_name, secret_key=db.password_secret_key
        ),
    }
    bindings = {
        item.name: database_fields[item.source.field]
        for item in env
        if item.source.kind == "DATABASE"
    }
    return SharedDeploymentResult(
        environment=request.environment,
        provider=request.provider,
        namespace=request.namespace,
        database=RenderedDatabase(
            release_name=request.release_names.db,
            helm_values=database_values,
            manifest_yaml=database_manifest,
            service=service,
        ),
        application=RenderedApplication(
            release_name=request.release_names.app,
            helm_values=application_values_dict,
            manifest_yaml=application_manifest,
        ),
        bindings=bindings,
    )
