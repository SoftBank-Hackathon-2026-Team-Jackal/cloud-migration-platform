from cmp_api.db import SessionLocal
from cmp_contracts.queues import PROVISIONING_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.activities.aws_infra import AwsInfraActivities
from cmp_worker.activities.provisioning import prepare_plan, provision_target
from cmp_worker.config import settings


def create_worker(client: Client) -> Worker:
    approval = ApprovalActivities(SessionLocal)
    aws_infra = AwsInfraActivities(
        SessionLocal,
        artifact_root=settings.provisioning_artifact_root,
        aws_profile=settings.provisioning_aws_profile,
        terraform_bin=settings.terraform_bin,
    )
    return Worker(
        client,
        task_queue=PROVISIONING_QUEUE,
        activities=[
            prepare_plan,
            provision_target,
            approval.claim_approved_execution,
            approval.complete_approved_execution,
            aws_infra.execute_aws_plan,
        ],
    )
