import os
from functools import lru_cache
from urllib.parse import quote

from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict

from shared.config import PROJECT_ROOT, SharedSettings, resolve_secret


class OrderSettings(SharedSettings):
    """
    Dedicated Order Settings.

    Inherits the shared application/Redis/RabbitMQ settings and adds the Order
    Service PostgreSQL connection details. Credentials are read from the files
    under .secrets/postgres/order-service/ (or /run/secrets/ in containers).
    """

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Order Service Database (Primary) --- #
    POSTGRES_HOST: str = Field(
        default="order-db-service",
        validation_alias=AliasChoices("ORDER_POSTGRES_HOST", "POSTGRES_HOST"),
        description="Order Service database host.",
    )
    POSTGRES_PORT: int = Field(
        default=5432,
        validation_alias=AliasChoices("ORDER_POSTGRES_PORT", "POSTGRES_PORT"),
        description="Order Service database port.",
    )
    POSTGRES_USER: str = Field(
        default="postgres",
        validation_alias=AliasChoices("ORDER_POSTGRES_USER", "POSTGRES_USER"),
        description="Database user (inline fallback).",
    )
    POSTGRES_USER_FILE: str | None = Field(
        default=str(PROJECT_ROOT / ".secrets" / "postgres" / "order-service" / "postgres_user"),
        validation_alias=AliasChoices("ORDER_POSTGRES_USER_FILE", "POSTGRES_USER_FILE"),
        description="Path to a file holding the database user.",
    )
    POSTGRES_PASSWORD: str = Field(
        default="postgres",
        validation_alias=AliasChoices("ORDER_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
        description="Database password (inline fallback).",
    )
    POSTGRES_PASSWORD_FILE: str | None = Field(
        default=str(PROJECT_ROOT / ".secrets" / "postgres" / "order-service" / "postgres_password"),
        validation_alias=AliasChoices("ORDER_POSTGRES_PASSWORD_FILE", "POSTGRES_PASSWORD_FILE"),
        description="Path to a file holding the database password.",
    )
    POSTGRES_DB: str = Field(
        default="order_service_db",
        validation_alias=AliasChoices("ORDER_POSTGRES_DB", "POSTGRES_DB"),
        description="Order Service database name.",
    )
    DATABASE_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ORDER_DATABASE_URL", "DATABASE_URL"),
        description="Explicit database URL override; built from the POSTGRES_* settings when unset.",
    )

    # Docker Compose service names for the shared infrastructure.
    REDIS_HOST: str = Field(default="redis-service", description="Redis host.")
    RABBITMQ_HOST: str = Field(default="rabbitmq-service", description="RabbitMQ host.")

    # --- Warehouse Service (live pricing) --- #
    WAREHOUSE_SERVICE_URL: str = Field(
        default="http://warehouse-service:8000",
        validation_alias=AliasChoices("ORDER_WAREHOUSE_SERVICE_URL", "WAREHOUSE_SERVICE_URL"),
        description="Base URL of the Warehouse Service used for live price lookups.",
    )
    WAREHOUSE_TIMEOUT_SECONDS: float = Field(
        default=2.0,
        description="Per-request timeout for warehouse price lookups.",
    )
    # Last-known prices are cached so intake survives a warehouse blip.
    PRICE_CACHE_TTL_SECONDS: int = Field(
        default=86400,
        description="How long a fetched product price stays usable as a fallback.",
    )

    # --- Idempotency --- #
    IDEMPOTENCY_EXPIRE_SECONDS: int = Field(
        default=86400, description="How long a completed idempotent response is replayed."
    )
    IDEMPOTENCY_WAIT_SECONDS: float = Field(
        default=10.0,
        description="How long a duplicate request waits for the in-flight original to finish.",
    )

    # --- Server --- #
    HOST: str = Field(default="0.0.0.0", description="Server host address.")
    PORT: int = Field(default=8000, description="Server port number.")
    CORS_ORIGINS: list[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        description="Browser origins allowed to call this API directly.",
    )

    @property
    def database_user(self) -> str:
        """Database user, read from POSTGRES_USER_FILE when configured."""
        return resolve_secret(self.POSTGRES_USER, self.POSTGRES_USER_FILE) or self.POSTGRES_USER

    @property
    def database_password(self) -> str:
        """Database password, read from POSTGRES_PASSWORD_FILE when configured."""
        return (
            resolve_secret(self.POSTGRES_PASSWORD, self.POSTGRES_PASSWORD_FILE)
            or self.POSTGRES_PASSWORD
        )

    @property
    def database_name(self) -> str:
        """Name of the Order Service database."""
        return self.POSTGRES_DB

    @property
    def database_url(self) -> str:
        """Build the async SQLAlchemy URL from the individual settings."""
        if self.DATABASE_URL:
            return self.DATABASE_URL

        user = quote(self.database_user, safe="")
        password = quote(self.database_password, safe="")

        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.database_name}"
        )


@lru_cache
def get_order_settings():
    return OrderSettings()
