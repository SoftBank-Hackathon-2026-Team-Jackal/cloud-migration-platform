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


settings = Settings()
