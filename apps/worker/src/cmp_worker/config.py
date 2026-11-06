from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # TLS to Temporal (the relay-host worker reaches the demo host through the HTTPS edge).
    temporal_tls: bool = False
    # Ticket 08: protected directory holding Planning output `<plan_id>-r<revision>/`.
    provisioning_artifact_root: str | None = None
    provisioning_aws_profile: str | None = None
    # Ticket 47 (D-10): comma-separated CIDRs the worker reaches new cluster APIs from (the demo
    # host EIP). AWS Plans refuse API access CIDRs that leave them out; empty = not checked.
    provisioning_platform_egress_cidrs: str | None = None
    terraform_bin: str = "terraform"
    # Ticket 03: directory for analysis source workspaces; must be outside the service directory.
    analysis_workspace_root: str | None = None
    # Ticket 19: immutable store for analysis results (hash-named files) and where the catalog and
    # skills live (defaults to the repository checkout), plus the region for the AWS scan.
    analysis_artifact_store_dir: str | None = None
    analysis_resources_dir: str | None = None
    analysis_aws_region: str | None = "ap-northeast-2"
    # Development only: lets an analysis input name a repository on this machine (for a private
    # app that has no public URL). Off by default: an input must never choose which local files
    # a worker reads in a shared deployment.
    analysis_allow_local_repositories: bool = False
    # Where a private repository's token comes from: `none` (default; public repositories only),
    # `env` (development: CMP_GITHUB_TOKEN in the worker's environment). The platform's token
    # store, once GitHub login exists, is another value of this setting.
    source_token_resolver: str = "none"
    # Ticket 20 (ADR-004): Claude. Off unless explicitly enabled; the API key is only ever read
    # by the SDK from ANTHROPIC_API_KEY. Spend limits are enforced by a shared ledger file.
    llm_enabled: bool = False
    llm_model: str = "claude-sonnet-5-5"
    llm_max_output_tokens: int = 16000
    llm_ledger_path: str | None = None
    llm_kill_switch_path: str | None = None
    llm_limit_run_usd: float = 3.0
    llm_limit_day_usd: float = 20.0
    llm_limit_month_usd: float = 100.0
    # Ticket 22: the Sandbox worker. Real loop Activities are registered only when the kubeconfig
    # of the dedicated `k3d-cmp-sandbox` cluster and the analysis store are set.
    sandbox_kubeconfig: str | None = None
    sandbox_workspace_root: str | None = (
        None  # fixed copies of the source; default: next to analysis
    )
    sandbox_helm_timeout: str = "5m"
    # Application images that already exist, as JSON {"<resource id>": "<repo>@sha256:<digest>"}.
    # Until the real Build is placed (D-12) this is how the loop gets its application images.
    sandbox_prebuilt_images: str | None = None
    # Ticket 25: the real build path. All four (profile is optional) switch it on; none keeps the
    # pre-built images above; some is refused at start-up. Only names, never a credential.
    build_source_bucket: str | None = None
    build_ecr_repository: str | None = None
    build_log_bucket: str | None = None
    build_platform: str | None = None
    build_aws_profile: str | None = None
    # Ticket 10: gcloud configuration and the expected Google ADC principal for GCP Apply.
    provisioning_gcloud_configuration: str | None = None
    provisioning_gcp_adc_email: str | None = None

    def platform_egress_cidrs(self) -> tuple[str, ...]:
        raw = self.provisioning_platform_egress_cidrs or ""
        return tuple(part.strip() for part in raw.split(",") if part.strip())


settings = Settings()
