"""Worker for the Sandbox activity queue."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from cmp_contracts.queues import SANDBOX_QUEUE
from cmp_providers.ecr_pull import PlatformRegistry, boto3_minter
from temporalio.client import Client
from temporalio.worker import Worker

from cmp_worker.activities import sandbox
from cmp_worker.activities.k8s_workloads_cli import CommandRunner
from cmp_worker.activities.sandbox_loop import SandboxLoopActivities, make_proxy_suite_runner
from cmp_worker.artifact_store import LocalArtifactStore
from cmp_worker.build_request import FileBuildStore
from cmp_worker.build_wiring import BuildSettings, ImageBuildAdapter, upload_suite
from cmp_worker.prebuilt_builder import PrebuiltImages
from cmp_worker.redteam_cache import optional_s3_cache
from cmp_worker.redteam_wiring import build_redteam_kit
from cmp_worker.sandbox_deploy import SANDBOX_CONTEXT, SandboxCluster, SandboxService
from cmp_worker.sandbox_events import sbx_emit
from cmp_worker.source_archive import AwsCliArchiveStore

if TYPE_CHECKING:
    from cmp_worker.config import Settings


def build_sandbox_loop(config: "Settings | None" = None) -> SandboxLoopActivities | None:
    """The real Sandbox loop needs the dedicated cluster's kubeconfig and the analysis store (the
    same directory the analysis worker writes to). Without both, only the scaffold runs.

    Raises ValueError at start-up for a configuration that can only fail later, such as application
    images that are not pinned by digest.
    """
    # Imported here, not at module level: reading the settings must not happen when a Workflow
    # sandbox imports this module (the settings read files, which a Workflow may not do).
    from cmp_api.env_store import optional_env_file_store

    from cmp_worker.config import settings
    from cmp_worker.llm_factory import build_llm_factory

    config = config or settings
    if not config.sandbox_kubeconfig or not config.analysis_artifact_store_dir:
        return None
    kubeconfig = Path(config.sandbox_kubeconfig).expanduser()
    if not kubeconfig.is_file():
        raise ValueError("SANDBOX_KUBECONFIG_NOT_FOUND")
    from cmp_providers.resources import resource_root  # ticket 43: CMP_RESOURCE_ROOT in the image

    repo_root = resource_root()  # resolved here: a Workflow import may not
    resources = Path(config.analysis_resources_dir) if config.analysis_resources_dir else repo_root
    workspace_root = Path(
        config.sandbox_workspace_root
        or Path(config.analysis_workspace_root or "/tmp/cmp-analysis") / "sandbox-fixes"
    )
    service = SandboxService(
        SandboxCluster(CommandRunner(), kubeconfig=kubeconfig, context=SANDBOX_CONTEXT),
        database_chart_dir=resources / "infra/helm/postgres",
        application_chart_dir=resources / "infra/helm/application",
        helm_timeout=config.sandbox_helm_timeout,
    )
    build_settings = BuildSettings.from_values(
        config.build_source_bucket,
        config.build_ecr_repository,
        config.build_log_bucket,
        config.build_platform,
        config.build_aws_profile,
    )
    build: Any = PrebuiltImages.from_json(config.sandbox_prebuilt_images)
    if build_settings is not None:
        build = ImageBuildAdapter(
            build_settings,
            archive_store=AwsCliArchiveStore(profile=build_settings.aws_profile),
            # Reuse a build of the same source across worker restarts (redeploys clear memory).
            build_store=FileBuildStore(Path(config.analysis_artifact_store_dir) / "build-cache"),
        )
    uploader = None
    pull_registry = minter = None
    if build_settings is not None:
        # Ticket 26: images the real build pushes are in the platform ECR, which needs a pull Secret.
        host, _, repository = build_settings.ecr_repository.partition("/")
        pull_registry = PlatformRegistry.parse(host, repository)
        minter = boto3_minter(build_settings.aws_profile)
        archives = build.archive_store
        bucket = build_settings.source_bucket
        uploader = lambda bundle: upload_suite(archives, bucket, bundle)
    store = LocalArtifactStore(config.analysis_artifact_store_dir)
    llm_factory = build_llm_factory(config)
    # Ticket 39 draft: None (no bucket configured) unless the operator sets one -- the same
    # "off unless set" shape as `sandbox_prebuilt_images`. Reuses the Build module's own profile
    # rather than defaulting to the environment's own: AGENTS.md asks that AWS access never be
    # silent about which named profile it uses.
    redteam_cache = optional_s3_cache(
        config.redteam_cache_bucket,
        prefix=config.redteam_cache_prefix,
        profile=build_settings.aws_profile if build_settings is not None else None,
    )
    return SandboxLoopActivities(
        suite_uploader=uploader,
        pull_registry=pull_registry,
        token_minter=minter,
        env_store=optional_env_file_store(),  # ENV-06: None until the platform store is set
        service=service,
        store=store,
        workspace_root=workspace_root,
        build=build,
        suite_runner=make_proxy_suite_runner(service),
        llm_factory=llm_factory,
        # Ticket 38: the red team exists only where the model is switched on (ADR-004 limits and
        # key). A run still needs `redteam` in its input and a person's yes to start.
        redteam=(
            build_redteam_kit(service, store, llm_factory, cache=redteam_cache)
            if llm_factory
            else None
        ),
        skills_root=resources / "skills",  # ticket 27-B: the Tester's `test-authoring` skill
    )


def create_worker(client: Client, loop: SandboxLoopActivities | None = None) -> Worker:
    """`loop` is built from settings when it is not given (see `build_sandbox_loop`)."""
    loop = loop or build_sandbox_loop()
    return Worker(
        client,
        task_queue=SANDBOX_QUEUE,
        activities=[
            *(loop.all() if loop is not None else []),
            sbx_emit,  # Provisioning 59: Workflow-level sandbox events
            sandbox.sandbox_queue_probe,
            sandbox.request_build,
            sandbox.prepare_sandbox,
            sandbox.render_sandbox,
            sandbox.apply_sandbox,
            sandbox.run_sandbox_tests,
            sandbox.judge_sandbox,
            sandbox.classify_sandbox_failure,
            sandbox.fix_sandbox,
            sandbox.publish_final_report,
            sandbox.teardown_sandbox,
        ],
    )
