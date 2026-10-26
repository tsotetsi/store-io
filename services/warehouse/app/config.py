import os
from functools import lru_cache
from urllib.parse import quote

from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict

from shared.config import PROJECT_ROOT, SharedSettings, resolve_secret


class WarehouseSettings(SharedSettings):
    """
    Dedicated Warehouse Settings.

    Inherits the shared application/Redis/RabbitMQ settings and adds the
    Warehouse Service PostgreSQL connection details. Credentials are read from
    the files under .secrets/postgres/warehouse-service/ (or /run/secrets/ in
    containers).
    """

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Warehouse Service Database (Primary) --- #
    POSTGRES_HOST: str = Field(
        default="warehouse-db-service",
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_HOST", "POSTGRES_HOST"),
        description="Warehouse Service database host.",
    )
    POSTGRES_PORT: int = Field(
        default=5432,
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_PORT", "POSTGRES_PORT"),
        description="Warehouse Service database port.",
    )
    POSTGRES_USER: str = Field(
        default="postgres",
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_USER", "POSTGRES_USER"),
        description="Database user (inline fallback).",
    )
    POSTGRES_USER_FILE: str | None = Field(
        default=str(PROJECT_ROOT / ".secrets" / "postgres" / "warehouse-service" / "postgres_user"),
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_USER_FILE", "POSTGRES_USER_FILE"),
        description="Path to a file holding the database user.",
    )
    POSTGRES_PASSWORD: str = Field(
        default="postgres",
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
        description="Database password (inline fallback).",
    )
    POSTGRES_PASSWORD_FILE: str | None = Field(
        default=str(
            PROJECT_ROOT / ".secrets" / "postgres" / "warehouse-service" / "postgres_password"
        ),
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_PASSWORD_FILE", "POSTGRES_PASSWORD_FILE"),
        description="Path to a file holding the database password.",
    )
    POSTGRES_DB: str = Field(
        default="warehouse_service_db",
        validation_alias=AliasChoices("WAREHOUSE_POSTGRES_DB", "POSTGRES_DB"),
        description="Warehouse Service database name.",
    )
    DATABASE_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WAREHOUSE_DATABASE_URL", "DATABASE_URL"),
        description="Explicit database URL override; built from the POSTGRES_* settings when unset.",
    )

    # Docker Compose service names for the shared infrastructure.
    REDIS_HOST: str = Field(default="redis-service", description="Redis host.")
    RABBITMQ_HOST: str = Field(default="rabbitmq-service", description="RabbitMQ host.")

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
        """Name of the Warehouse Service database."""
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
def get_warehouse_settings():
    return WarehouseSettings()
