"""ON_PREM environment teardown Agent work (ticket 52, decisions D7–D9).

The target is derived from an earlier `PRODUCTION_ROLLOUT` job of the same Agent; a request can
never name an arbitrary Namespace. Only names cross this boundary. `delete_data` additionally
removes the database PVC and the Namespace itself; it is never set by an automatic path.
"""

import hashlib
from typing import Literal

from pydantic import Field

from cmp_contracts.chart import KubernetesName
from cmp_contracts.onprem_rollout import ReleaseName
from cmp_contracts.provisioning import Contract, Text

TEARDOWN_MODE = "ENVIRONMENT_TEARDOWN"


class TeardownJob(Contract):
    mode: Literal["ENVIRONMENT_TEARDOWN"]
    job_id: KubernetesName
    source_job_id: KubernetesName
    environment_id: Text
    agent_id: KubernetesName
    cluster_uid: Text
    namespace: KubernetesName
    # Removal order: application first, then the database.
    releases: list[ReleaseName] = Field(min_length=1, max_length=2)
    # The DB PVC carries the DB release name; its password Secret is kept with the data.
    database_release: ReleaseName
    database_secret: KubernetesName
    delete_data: bool


def teardown_job_id(source_job_id: str, delete_data: bool) -> str:
    """One teardown job per (rollout job, data decision); retries reuse the same ID."""
    seed = f"{source_job_id}:{'delete' if delete_data else 'keep'}"
    return "teardown-" + hashlib.sha256(seed.encode()).hexdigest()[:24]
