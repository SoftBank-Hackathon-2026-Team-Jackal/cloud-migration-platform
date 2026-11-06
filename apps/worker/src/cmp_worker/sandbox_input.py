"""Builds the Sandbox loop input from what the analysis produced (ticket 21).

Pure and deterministic: the same analysis result and settings give the same input, and the
test suite it stores has the same SHA-256. Only references, names and public image digests leave
here; the source stays in the workspace on this worker.
"""

from typing import Any, Literal

from cmp_contracts.analysis import AnalysisReport
from cmp_contracts.provisioning import ContentRef, Image
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_testsuite import dump_bundle
from cmp_testsuite.model import Suite
from cmp_testsuite.standard import AppApi, standard_cases
from pydantic import BaseModel, ConfigDict, Field

from cmp_worker.artifact_store import ArtifactRef, LocalArtifactStore

# Official postgres:16 pinned by digest (D-11 is still open: nobody owns the choice yet).
POSTGRES_16 = Image(
    uri="docker.io/library/postgres@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a",
    platforms=["linux/amd64", "linux/arm64"],
)


class SandboxSettings(BaseModel):
    """What the platform, not the analysis, decides about this Sandbox run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    database_image: Image = POSTGRES_16
    storage_class: str = "local-path"
    region: str = "ap-northeast-2"
    env_names: list[str] = Field(default_factory=list)
    env_file: str | None = None
    env_file_ref: ContentRef | None = None  # ENV-02: passed through; ENV-06 renders from it
    source_hosts: list[str] = Field(default_factory=list)
    extra_keep_hosts: list[dict[str, Any]] = Field(default_factory=list)
    # How the standard write -> read test reaches the app; without it only the health test runs.
    ai_tests: bool = False
    write_path: str | None = None
    read_path: str | None = None
    # The app's write API shape (ticket 27); see `AppApi`. Left out, the original shape is used.
    write_body: dict[str, Any] | None = None
    write_status: int | None = None
    id_from_response: str | None = None
    value_field: str = "value"
    read_back: Literal["GET", "REPEAT_WRITE"] = "GET"
    repeat_status: int = 200


def keep_hosts(report: AnalysisReport | None, region: str) -> list[dict[str, Any]]:
    """Services left at the source are reachable through the proxy (first cycle: S3)."""
    kept = {r.capability for r in (report.mapping if report else []) if r.disposition == "KEEP"}
    hosts: list[dict[str, Any]] = []
    if "object-storage" in kept:
        hosts += [
            {"host": f"s3.{region}.amazonaws.com", "port": 443},
            {"host": f"*.s3.{region}.amazonaws.com", "port": 443},
        ]
    return hosts


def default_run_id(migration_id: str) -> str:
    import hashlib

    return "r" + hashlib.sha256(migration_id.encode()).hexdigest()[:10]


def assemble_sandbox_input(
    analysis: dict[str, Any], settings: SandboxSettings, store: LocalArtifactStore
) -> dict[str, Any]:
    """`analysis` is the analysis child's result: report, requirements, review and source."""
    requirements_ref = ArtifactRef.model_validate(analysis["requirements"])
    requirements = RequirementsV2.model_validate_json(store.get(requirements_ref))
    apps = [r for r in requirements.resources if r.kind == "APPLICATION"]
    if not apps:
        raise ValueError("NO_APPLICATION_IN_REQUIREMENTS")
    report_ref = analysis["report"].get("report_ref")
    report = (
        AnalysisReport.model_validate_json(store.get(ArtifactRef.model_validate(report_ref)))
        if report_ref
        else None
    )
    api = AppApi(
        health_path=apps[0].health_path,
        write_path=settings.write_path,
        read_path=settings.read_path,
        write_body=settings.write_body,
        write_status=settings.write_status,
        id_from_response=settings.id_from_response,
        value_field=settings.value_field,
        read_back=settings.read_back,
        repeat_status=settings.repeat_status,
        health_from_target=True,
    )
    migration_id = (report.migration_id if report else None) or analysis["report"].get(
        "migration_id"
    )
    if not migration_id:
        raise ValueError("MIGRATION_ID_MISSING")
    suite = Suite(name=f"standard-{migration_id}", cases=standard_cases(api))
    suite_ref = store.put(dump_bundle(suite), ref_id=f"suite-{migration_id}", kind="test-suite")
    review = analysis.get("review") or {}
    env_plan_ref = None
    if analysis.get("assembly_ref"):
        assembly = store.get_json(ArtifactRef.model_validate(analysis["assembly_ref"]))
        env_plan_ref = assembly.get("env_plan_ref")  # ENV-05: set when `.env` came by reference
    return {
        "migration_id": migration_id,
        "run_id": settings.run_id or default_run_id(migration_id),
        "requirements_ref": requirements_ref.model_dump(mode="json"),
        "suite_ref": suite_ref.model_dump(mode="json"),
        "source": {"workspace": analysis["source"]["workspace"]},
        "database_image": settings.database_image.model_dump(mode="json"),
        "storage_class": settings.storage_class,
        "env_names": settings.env_names,
        "env_file": settings.env_file,
        **({"env_plan_ref": env_plan_ref} if env_plan_ref else {}),
        **(
            {"env_file_ref": settings.env_file_ref.model_dump(mode="json")}
            if settings.env_file_ref
            else {}
        ),
        "keep_hosts": [*keep_hosts(report, settings.region), *settings.extra_keep_hosts],
        "source_hosts": settings.source_hosts,
        **({"ai_tests": True} if settings.ai_tests else {}),
        "analysis_report_ref": report_ref,
        "review_ref": review.get("review_ref"),
    }
