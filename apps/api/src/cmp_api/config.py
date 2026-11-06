from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    database_url: str = "postgresql+psycopg://platform:platform@localhost:5433/platform"
    agent_enrollment_admin_key: str | None = None
    approval_admin_key: str | None = None
    # Ticket 60 A12 (demo): an existing approver that `POST /migrations` grants each new Migration
    # (ticket 07 operator grant, per-Migration scope kept). Unset: starts grant nothing.
    demo_approver_id: str | None = None
    # P0 platform artifact store for approved ON_PREM rollout bytes (digest filenames; S3 later).
    artifact_store_dir: str | None = None
    # Ticket 23 (ADR-005): environment connections. The platform principal (demo: EC2 instance
    # role; locally a named dev profile) is the only identity that calls sts:AssumeRole.
    connection_platform_aws_profile: str | None = None
    connection_platform_principal_arn: str | None = None
    # HTTPS prefix of the S3-hosted CloudFormation templates (quick-create needs an S3 URL).
    connection_template_base_url: str | None = None
    # Generated AWS config (platform profiles + one assume-role profile per connection), shared
    # with the worker through AWS_CONFIG_FILE; the base file holds the platform's own profile.
    connection_aws_config_file: str | None = None
    connection_aws_base_config_file: str | None = None
    # GCP demo: the platform's fixed project; other projects are refused.
    connection_gcp_project_id: str | None = None
    # Ticket 25 (F1/F2): a tunnel report is current evidence only while it is recent (60 s = four
    # missed 15 s Agent reports) and its WireGuard handshake is within Reject-After-Time (180 s).
    tunnel_status_ttl_seconds: int = 60
    tunnel_handshake_stale_seconds: int = 180
    # Ticket 64: how long an ON_PREM traffic handover lookup waits for this rollout's tunnel
    # health check (cut to the Activity timeout), and how often it reads the tunnel status.
    traffic_handover_wait_seconds: int = 180
    traffic_handover_poll_seconds: float = 2.0
    # ENV-01: uploaded `.env` files, encrypted (AES-256-GCM) under this directory (files 0600).
    # The key is a base64url 32-byte value from the server's own `.env`, never the repository.
    env_file_store_dir: str | None = None
    env_file_encryption_key: SecretStr | None = None
    # Ticket 44 (S6): operator-managed target profile catalog (JSON). The web start request's
    # `target.profile` selects an entry; its values are the target settings defaults. Without it
    # a real (`target`) start is 503 `TARGET_PROFILES_UNCONFIGURED`.
    planning_target_profiles_file: str | None = None
    # Ticket 29 (I1): the platform's own ECR registry host and the repositories approved images
    # may come from (ticket 28 builds into `cmp-platform-builds`). The platform principal
    # (`connection_platform_aws_profile`, the instance role on the demo host) mints pull tokens.
    platform_ecr_registry: str | None = None
    platform_ecr_repositories: str = "cmp-platform-builds"
    # Ticket 46: the principal that mints those pull tokens, kept apart from the connection
    # principal so either can change alone (demo: both `cmp-demo-build`, the instance role with
    # the ticket-28 ECR policy). Unset: `connection_platform_aws_profile` (ticket-41 behavior).
    platform_ecr_aws_profile: str | None = None
    # Ticket 57 (#228): the rehearsal's standard write -> read shape for web (`target`) starts,
    # a `SandboxSettings` JSON object (write_path, write_body, read_back, ...; the worker checks
    # it). Operator configuration for the app being migrated, never code. Unset: `sandbox` stays
    # null and the rehearsal runs the health test only; not a JSON object: 503
    # `SANDBOX_SETTINGS_INVALID`.
    web_start_sandbox_settings: str | None = None
    # Data-migration ticket 08: the data step's input for web (`target`) starts, a
    # `DataMigrationInput` JSON object (source DB endpoint, source app, cutover settings, target
    # tunnel address; the CLI's `--data-migration`, no secrets). Operator configuration for the
    # app being migrated. Unset: no `data_migration`, so the data step stays the scaffold; not a
    # valid input: 503 `DATA_MIGRATION_SETTINGS_INVALID`.
    web_start_data_migration: str | None = None
    # #128: the traffic step's input for web (`target`) starts, a `TrafficShiftInput` JSON object
    # (relay worker queue, step policy; the CLI's `--traffic`, no secrets). Operator configuration
    # for the relay server. Unset: no `traffic`, so the traffic step stays the scaffold and the relay
    # is never touched; not a valid input: 503 `TRAFFIC_SETTINGS_INVALID`.
    web_start_traffic: str | None = None


settings = Settings()
