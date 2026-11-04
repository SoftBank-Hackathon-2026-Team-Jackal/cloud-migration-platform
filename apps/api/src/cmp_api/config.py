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


settings = Settings()
