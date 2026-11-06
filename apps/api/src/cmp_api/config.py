from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    database_url: str = "postgresql+psycopg://platform:platform@localhost:5433/platform"
    agent_enrollment_admin_key: str | None = None
    approval_admin_key: str | None = None
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
    # ENV-01: uploaded `.env` files, encrypted (AES-256-GCM) under this directory (files 0600).
    # The key is a base64url 32-byte value from the server's own `.env`, never the repository.
    env_file_store_dir: str | None = None
    env_file_encryption_key: SecretStr | None = None
    # Ticket 29 (I1): the platform's own ECR registry host and the repositories approved images
    # may come from (ticket 28 builds into `cmp-platform-builds`). The platform principal
    # (`connection_platform_aws_profile`, the instance role on the demo host) mints pull tokens.
    platform_ecr_registry: str | None = None
    platform_ecr_repositories: str = "cmp-platform-builds"
    # Ticket 46: the principal that mints those pull tokens when it is not the connection
    # principal (demo: `cmp-demo-build`, the instance role with the ticket-28 ECR policy; the
    # connector role `cmp-platform-connector` has no ECR access). Unset: ticket-41 behavior.
    platform_ecr_aws_profile: str | None = None


settings = Settings()
