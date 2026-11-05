"""Start the real first cycle without the API (demo CLI).

  uv run python scripts/start_migration.py --repository /path/to/demo-app --commit <sha> \
      --manifests-dir k8s --project-id demo-1 [--sandbox '{"write_path": "/api/messages"}']

Prints the Workflow ID. Then watch it and answer the review with:
  uv run python scripts/review_signal.py state <workflow id>
The analysis never reads a credential from here; `--credential-ref` names a local AWS profile.
"""

import argparse
import asyncio
import sys
from uuid import uuid4

from cmp_contracts.queues import WORKFLOW_QUEUE
from cmp_worker.config import settings
from cmp_worker.start_input import build_start_input
from cmp_worker.workflows.migration import MigrationWorkflow
from pydantic import ValidationError
from temporalio.client import Client


async def main(args: argparse.Namespace) -> int:
    try:
        data = build_start_input(
            project_id=args.project_id, repository=args.repository, commit=args.commit,
            manifests_dir=args.manifests_dir, cluster_name=args.cluster_name,
            credential_ref=args.credential_ref, sandbox_json=args.sandbox,
        )  # fmt: skip
    except (ValidationError, ValueError) as exc:
        print(f"Input is invalid: {type(exc).__name__}")
        return 2
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    workflow_id = args.workflow_id or f"migration-{uuid4()}"
    await client.start_workflow(
        MigrationWorkflow.run, data, id=workflow_id, task_queue=WORKFLOW_QUEUE
    )
    print(workflow_id)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--manifests-dir")
    parser.add_argument("--cluster-name")
    parser.add_argument("--credential-ref")
    parser.add_argument("--sandbox", help="SandboxSettings as JSON")
    parser.add_argument("--workflow-id")
    sys.exit(asyncio.run(main(parser.parse_args())))
