import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cmp_contracts.analysis import AnalysisReport, ReviewRecord
from cmp_contracts.provisioning_v2 import RequirementsV2
from cmp_providers.env_file import EnvFileError
from cmp_scanners.source import SourceFetchError
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from cmp_worker.analysis_pipeline import AnalysisPipeline
from cmp_worker.sandbox_input import SandboxSettings, assemble_sandbox_input
from cmp_worker.source_access import SourceAccessError


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


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking pipeline step off the event loop; map deterministic input errors to
    non-retryable failures so a bad repository or commit does not spin through retries."""
    try:
        return await asyncio.to_thread(fn, *args)
    except SourceAccessError as exc:
        # A missing, revoked or refused token will not fix itself on a retry.
        raise ApplicationError(
            "Source access was refused", type=exc.code, non_retryable=True
        ) from None
    except EnvFileError as exc:
        # An expired, missing or unconfigured `.env` will not fix itself on a retry (ENV-05).
        raise ApplicationError(
            "The uploaded .env could not be used", type=exc.code, non_retryable=True
        ) from None
    except SourceFetchError:
        raise ApplicationError(
            "Source could not be fetched", type="SOURCE_FETCH_FAILED", non_retryable=True
        ) from None
    except (ValueError, FileNotFoundError, ValidationError):
        raise ApplicationError(
            "Analysis input is invalid", type="ANALYSIS_INPUT_INVALID", non_retryable=True
        ) from None


class AnalysisActivities:
    """Real analysis Activities (ticket 19). Same names as the scaffold ones, JSON in and out."""

    def __init__(self, pipeline: AnalysisPipeline) -> None:
        self.pipeline = pipeline

    @activity.defn(name="fetch_source")
    async def fetch_source(self, inp: dict) -> dict:
        return await _run(self.pipeline.fetch_source, inp)

    @activity.defn(name="scan_code")
    async def scan_code(self, payload: dict) -> dict:
        return await _run(self.pipeline.scan_code, payload["input"], payload["source"])

    @activity.defn(name="scan_manifests")
    async def scan_manifests(self, payload: dict) -> dict:
        return await _run(self.pipeline.scan_manifests, payload["input"], payload["source"])

    @activity.defn(name="scan_aws_infra")
    async def scan_aws_infra(self, inp: dict) -> dict:
        return await _run(self.pipeline.scan_aws_infra, inp)

    @activity.defn(name="build_requirements")
    async def build_requirements(self, payload: dict) -> dict:
        return await _run(
            self.pipeline.build_requirements,
            payload["input"],
            payload["source"],
            payload["code_ref"],
            payload["manifests_ref"],
            payload["infra"],
        )

    @activity.defn(name="precheck")
    async def precheck(self, payload: dict) -> dict:
        return await _run(
            self.pipeline.precheck,
            payload["input"],
            payload["source"],
            payload["code_ref"],
            payload["manifests_ref"],
        )

    @activity.defn(name="map_usage")
    async def map_usage(self, payload: dict) -> dict:
        return await _run(
            self.pipeline.map_usage,
            payload["input"],
            payload["source"],
            payload["code_ref"],
            payload["manifests_ref"],
            payload["built"],
            payload["precheck"],
        )

    @activity.defn(name="check_evidence")
    async def check_evidence(self, payload: dict) -> dict:
        return await _run(
            self.pipeline.check_evidence,
            payload["input"],
            payload["source"],
            payload["built"],
            payload["infra"],
            payload["precheck"],
            payload["mapped"],
        )

    @activity.defn(name="record_review")
    async def record_review(self, payload: dict) -> dict:
        migration_id = payload["migration_id"]
        body = {k: v for k, v in payload.items() if k != "migration_id"}
        record = ReviewRecord.model_validate({**body, "decided_at": datetime.now(UTC)})
        ref = self.pipeline.store.put_json(
            record, ref_id=f"review-{migration_id}", kind="review-record"
        )
        return {**record.model_dump(mode="json"), "review_ref": ref.model_dump()}

    @activity.defn(name="assemble_sandbox_input")
    async def assemble_sandbox_input(self, payload: dict) -> dict:
        """Turn the analysis result into the Sandbox loop input (ticket 21)."""
        try:
            return await asyncio.to_thread(
                assemble_sandbox_input,
                payload["analysis"],
                SandboxSettings.model_validate(payload.get("settings") or {}),
                self.pipeline.store,
            )
        except (KeyError, ValueError, ValidationError):
            raise ApplicationError(
                "Sandbox input could not be assembled",
                type="SANDBOX_INPUT_INVALID",
                non_retryable=True,
            ) from None

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.assemble_sandbox_input,
            self.fetch_source,
            self.scan_code,
            self.scan_manifests,
            self.scan_aws_infra,
            self.build_requirements,
            self.precheck,
            self.map_usage,
            self.check_evidence,
            self.record_review,
        ]
