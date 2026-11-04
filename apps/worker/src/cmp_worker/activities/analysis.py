from datetime import UTC, datetime

from cmp_contracts.analysis import AnalysisReport, ReviewRecord
from cmp_contracts.provisioning_v2 import RequirementsV2
from temporalio import activity


@activity.defn
async def analyze_project(project_id: str) -> dict[str, str]:
    """Example boundary for source and application analysis; performs no scan."""
    activity.logger.info("Scaffold analysis for %s", project_id)
    return {"step": "analysis", "mode": "scaffold", "detail": "No cloud scan performed"}


@activity.defn
async def fetch_source(migration_id: str) -> str:
    return migration_id


@activity.defn
async def scan_code(migration_id: str) -> str:
    return "scaffold-code"


@activity.defn
async def scan_manifests(migration_id: str) -> str:
    return "scaffold-manifests"


@activity.defn
async def scan_aws_infra(migration_id: str) -> str:
    return "scaffold-aws-infra"


@activity.defn
async def scan_network(migration_id: str) -> str:
    return "scaffold-network"


@activity.defn
async def build_requirements(migration_id: str) -> dict:
    return RequirementsV2.model_validate(
        {
            "contract_version": "2",
            "resources": [
                {
                    "id": "app",
                    "kind": "APPLICATION",
                    "replicas": 1,
                    "cpu_millicores": 250,
                    "memory_mib": 256,
                    "port": 8080,
                    "health_path": "/health",
                }
            ],
        }
    ).model_dump(mode="json")


@activity.defn
async def precheck(migration_id: str) -> str:
    return "scaffold-precheck"


@activity.defn
async def map_usage(migration_id: str) -> str:
    return "scaffold-mapping"


@activity.defn
async def check_evidence(payload: dict) -> dict:
    RequirementsV2.model_validate(payload["requirements"])
    migration_id = payload["migration_id"]
    return AnalysisReport.model_validate(
        {
            "migration_id": migration_id,
            "ref": {"id": "analysis", "version": "1"},
            "base_commit": "fixture",
            "commit_verified": False,
            "usage_profile": {},
            "mapping": [],
            "precheck": {},
            "external_entries": [],
            "source_cidrs": [],
            "source_db_images": {},
            "unmapped_items": [],
            "unverified_risks": ["Scaffold: no scan performed"],
        }
    ).model_dump(mode="json")


@activity.defn
async def record_review(payload: dict) -> dict:
    """Stamp the user's decisions with the decision time. Immutable storage is decided in D-02."""
    return ReviewRecord.model_validate({**payload, "decided_at": datetime.now(UTC)}).model_dump(
        mode="json"
    )
