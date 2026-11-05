from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    # Ticket 08: protected directory holding Planning output `<plan_id>-r<revision>/`.
    provisioning_artifact_root: str | None = None
    provisioning_aws_profile: str | None = None
    terraform_bin: str = "terraform"
    # Ticket 03: directory for analysis source workspaces; must be outside the service directory.
    analysis_workspace_root: str | None = None
    # Ticket 19: immutable store for analysis results (hash-named files) and where the catalog and
    # skills live (defaults to the repository checkout), plus the region for the AWS scan.
    analysis_artifact_store_dir: str | None = None
    analysis_resources_dir: str | None = None
    analysis_aws_region: str | None = "ap-northeast-2"
    # Ticket 10: gcloud configuration and the expected Google ADC principal for GCP Apply.
    provisioning_gcloud_configuration: str | None = None
    provisioning_gcp_adc_email: str | None = None


settings = Settings()
