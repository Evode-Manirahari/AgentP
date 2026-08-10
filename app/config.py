from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPDF_", env_file=".env", extra="ignore")

    app_name: str = "AgentP Document Execution API"
    api_key: str = "local-dev-key"

    database_url: str = "postgresql+psycopg://agentpdf:local-password@postgres:5432/agentpdf"
    redis_url: str = "redis://redis:6379/0"
    queue_name: str = "pdf-jobs"

    s3_endpoint_url: str = "http://minio:9000"
    s3_public_endpoint_url: str | None = None
    s3_access_key_id: str = "agentpdf"
    s3_secret_access_key: str = "agentpdf-secret"
    s3_bucket: str = "agentpdf"
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False

    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_pages: int = Field(default=500, gt=0)
    operation_timeout_seconds: int = Field(default=300, gt=0)
    qpdf_timeout_seconds: int = Field(default=30, gt=0)
    download_url_expires_seconds: int = Field(default=900, gt=0)
    webhook_delivery_timeout_seconds: int = Field(default=10, gt=0)
    webhook_max_attempts: int = Field(default=3, gt=0)
    webhook_allow_private_urls: bool = False
    document_retention_days: int | None = Field(default=None, gt=0)
    temp_prefix: str = "agentp-"


@lru_cache
def get_settings() -> Settings:
    return Settings()
