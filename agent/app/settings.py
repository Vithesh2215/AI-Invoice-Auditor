from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ai-invoice-auditor-agent"
    app_env: str = Field(default="production")
    log_level: str = Field(default="INFO")
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    agent_db_host: str = "postgres"
    agent_db_port: int = 5432
    agent_db_name: str = "invoice_auditor"
    agent_db_user: str = "invoice_agent"
    agent_db_password: SecretStr
    db_pool_min_size: int = 1
    db_pool_max_size: int = 8
    db_connect_timeout: int = 5
    db_statement_timeout_ms: int = 30_000

    erp_base_url: str = "http://mock-erp:8001"
    erp_timeout_seconds: float = 10.0

    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "invoice_chunks"
    qdrant_timeout_seconds: float = 20.0

    redis_url: str = "redis://redis:6379/0"
    redis_timeout_seconds: float = 3.0
    llm_cache_ttl_seconds: int = 60 * 60 * 24 * 7
    translation_cache_ttl_seconds: int = 60 * 60 * 24 * 30

    aws_region: str = "us-east-1"
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None

    llm_reasoning_model: str = "bedrock/converse/global.amazon.nova-2-lite-v1:0"
    llm_translation_model: str = "bedrock/converse/global.amazon.nova-2-lite-v1:0"
    llm_rag_model: str = "bedrock/converse/global.amazon.nova-2-lite-v1:0"
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 3
    llm_enabled: bool = True
    llm_circuit_breaker_seconds: float = 60.0
    llm_health_probe_interval_seconds: float = 300.0

    embedding_provider: str = "bedrock"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    bedrock_embedding_model: str = "amazon.titan-embed-text-v1"
    embedding_dim: int = 1536
    embedding_cache_dir: str = "/opt/models"

    incoming_dir: Path = Path("/data/incoming")
    reports_dir: Path = Path("/data/reports")
    config_dir: Path = Path("/config")

    watch_enabled: bool = True
    watch_interval_seconds: float = 10.0
    watch_stability_seconds: float = 2.0
    watch_max_concurrency: int = 2
    supported_extensions: tuple[str, ...] = (".pdf", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff")

    ocr_languages: str = "eng+spa+deu"
    ocr_dpi: int = 300
    ocr_min_text_chars: int = 40

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("aws_access_key_id", "aws_secret_access_key", "aws_session_token", mode="before")
    @classmethod
    def _blank_credential_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("embedding_provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        value = value.lower()
        if value not in {"fastembed", "bedrock"}:
            raise ValueError("embedding_provider must be 'fastembed' or 'bedrock'")
        return value

    @property
    def rules_path(self) -> Path:
        return self.config_dir / "rules.yaml"

    @property
    def personas_path(self) -> Path:
        return self.config_dir / "personas.yaml"

    @property
    def prompts_path(self) -> Path:
        return self.config_dir / "prompts.yaml"

    def db_connection_kwargs(self) -> dict:
        return {
            "host": self.agent_db_host,
            "port": self.agent_db_port,
            "dbname": self.agent_db_name,
            "user": self.agent_db_user,
            "password": self.agent_db_password.get_secret_value(),
            "connect_timeout": self.db_connect_timeout,
            "options": (
                f"-c statement_timeout={self.db_statement_timeout_ms} "
                "-c search_path=audit,langgraph,public"
            ),
        }

    def db_uri(self) -> str:
        from urllib.parse import quote_plus

        password = quote_plus(self.agent_db_password.get_secret_value())
        return (
            f"postgresql://{quote_plus(self.agent_db_user)}:{password}"
            f"@{self.agent_db_host}:{self.agent_db_port}/{self.agent_db_name}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
