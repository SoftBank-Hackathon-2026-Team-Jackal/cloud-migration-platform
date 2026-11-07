from cmp_api.db import SessionLocal
from cmp_contracts.queues import PROVISIONING_QUEUE
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities.approval import ApprovalActivities
from cmp_worker.activities.aws_infra import AwsInfraActivities
from cmp_worker.activities.aws_workloads_cli import build_aws_workloads_activities
from cmp_worker.activities.cloud_teardown import build_cloud_teardown_activities
from cmp_worker.activities.gcp_connection import verify_gcp_connection
from cmp_worker.activities.gcp_infra import GcpInfraActivities
from cmp_worker.activities.gcp_workloads_cli import build_gcp_workloads_activities
from cmp_worker.activities.onprem_execution import execute_on_prem_plan_activity
from cmp_worker.activities.plan_publish import build_plan_publish_activities
from cmp_worker.activities.planning_register import build_planning_register_activities
from cmp_worker.activities.provisioning import prepare_plan, provision_target
from cmp_worker.activities.shadow_explain import shadow_explain
from cmp_worker.config import settings
from cmp_worker.events import emit_migration_event


def create_worker(client: Client) -> Worker:
    approval = ApprovalActivities(SessionLocal)
    aws_infra = AwsInfraActivities(
        SessionLocal,
        artifact_root=settings.provisioning_artifact_root,
        aws_profile=settings.provisioning_aws_profile,
        terraform_bin=settings.terraform_bin,
    )
    gcp_infra = GcpInfraActivities(
        SessionLocal,
        artifact_root=settings.provisioning_artifact_root,
        aws_profile=settings.provisioning_aws_profile,
        gcloud_configuration=settings.provisioning_gcloud_configuration,
        adc_email=settings.provisioning_gcp_adc_email,
        terraform_bin=settings.terraform_bin,
    )
    plans = build_plan_publish_activities(SessionLocal)
    gcp_workloads = build_gcp_workloads_activities(SessionLocal)
    teardown = build_cloud_teardown_activities(SessionLocal)  # ticket 53
    return Worker(
        client,
        task_queue=PROVISIONING_QUEUE,
        activities=[
            prepare_plan,
            provision_target,
            build_planning_register_activities(SessionLocal).register_planning_input,  # ticket 15
            plans.publish_migration_plan,  # ticket 18
            plans.publish_workload_plan,  # ticket 22
            approval.claim_approved_execution,
            approval.complete_approved_execution,
            approval.get_migration_handoff,  # ticket 13
            approval.get_traffic_handover,  # ticket 50
            aws_infra.execute_aws_plan,
            build_aws_workloads_activities(SessionLocal).execute_aws_workloads,
            gcp_workloads.execute_gcp_workloads,  # ticket 11
            gcp_workloads.refresh_gcp_pull_secrets,  # ticket 29: Schedule-driven refresh
            execute_on_prem_plan_activity(),  # ticket 12
            gcp_infra.execute_gcp_plan,  # ticket 10
            teardown.teardown_cloud_workloads,  # ticket 53: operator-started cloud teardown
            teardown.plan_cloud_destroy,
            teardown.apply_cloud_destroy,
            emit_migration_event,  # ticket 24: Workflow-side event emission
            shadow_explain,  # Provisioning 84: 그림자 리허설 불일치 설명(제안만)
            verify_gcp_connection,  # ticket 48: GCP_TARGET connection check
        ],
    )
