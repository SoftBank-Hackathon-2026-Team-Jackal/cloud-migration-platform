from pathlib import Path

from cmp_contracts.queues import ANALYSIS_QUEUE
from cmp_scanners.aws_infra import get_session, make_clients
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.analysis import (
    AnalysisActivities,
    analyze_project,
    build_requirements,
    check_evidence,
    fetch_source,
    map_usage,
    precheck,
    record_review,
    scan_aws_infra,
    scan_code,
    scan_manifests,
    scan_network,
)
from cmp_worker.analysis_pipeline import AnalysisPipeline
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.config import Settings
from cmp_worker.config import settings as default_settings

_REPO_ROOT = Path(__file__).resolve().parents[5]


def build_pipeline(config: Settings = default_settings) -> AnalysisPipeline | None:
    """Real analysis steps need a result store and a workspace; otherwise the scaffold runs."""
    if not config.analysis_artifact_store_dir or not config.analysis_workspace_root:
        return None
    resources = Path(config.analysis_resources_dir) if config.analysis_resources_dir else _REPO_ROOT
    region = config.analysis_aws_region
    return AnalysisPipeline(
        store=LocalArtifactStore(config.analysis_artifact_store_dir),
        workspace_root=config.analysis_workspace_root,
        skills_root=resources / "skills",
        catalog_dir=resources / "catalog",
        llm_factory=None,  # the Claude client is connected after ADR-004
        aws_clients_factory=lambda inp: make_clients(
            get_session(inp.credential_ref or "", region=region)
        ),
    )


def create_worker(client: Client, pipeline: AnalysisPipeline | None = None) -> Worker:
    pipeline = pipeline or build_pipeline()
    if pipeline is not None:
        return Worker(
            client, task_queue=ANALYSIS_QUEUE, activities=AnalysisActivities(pipeline).all()
        )
    return Worker(
        client,
        task_queue=ANALYSIS_QUEUE,
        activities=[
            analyze_project,
            fetch_source,
            scan_code,
            scan_manifests,
            scan_aws_infra,
            scan_network,
            build_requirements,
            precheck,
            map_usage,
            check_evidence,
            record_review,
        ],
    )
