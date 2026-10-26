"""
Shared application settings for all Store-IO Services.
"""

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)

# Repository root (services/shared/config.py -> services/shared -> services -> root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = PROJECT_ROOT / ".secrets"


def _resolve_path(file_path: str | os.PathLike[str]) -> Path:
    """Resolve a possibly-relative path against the repository root."""
    path = Path(file_path).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_secret(file_path: str | os.PathLike[str] | None) -> str | None:
    """
    Read a secret from a file, returning None when it cannot be read.
    """
    if not file_path:
        return None

    path = _resolve_path(file_path)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Failed to read secret file", path=str(path), error=str(exc))
    return None


def resolve_secret(
    value: str | None, file_path: str | os.PathLike[str] | None = None
) -> str | None:
    """
    Resolve a secret value, preferring the content of ``file_path``.
    """
    file_value = read_secret(file_path)
    if file_value:
        return file_value

    # Backwards compatible: a bare path in the value field is read as a file.
    if value and isinstance(value, str) and value.startswith(("/run/secrets/", ".secrets/")):
        secret_value = read_secret(value)
        if secret_value:
            return secret_value
        logger.warning("Secret file path provided but file not found", path=value)

    return value


class SharedSettings(BaseSettings):
    """
    General Application Settings loaded from environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application --- #
    APP_NAME: str = Field(default="Store IO", description="Name of the application.")
    APP_VERSION: str = Field(default="Store IO", description="Version of the application.")
    LOG_LEVEL: str = Field(default="INFO", description="Log level for the application.")

    ENVIRONMENT: str = Field(default="development", description="Environment of the application.")
    DEBUG: bool = Field(default=True, description="Enable debug mode.")

    # Fault Injection Settings(Chaos Monkey).
    ENABLE_GLITCH_SIMULATION: bool = Field(default=False, description="Enable fault injection.")
    GLITCH_PROBABILITY: float = Field(default=0.2, description="20% chance of failure if enabled.")
    GLITCH_MAX_DELAY_SECONDS: float = Field(
        default=4.0, description="Maximum delay in seconds for fault injection."
    )

    # --- Transactional outbox --- #
    OUTBOX_POLL_INTERVAL_SECONDS: float = Field(
        default=0.5, description="How often the outbox publisher looks for unpublished events."
    )
    OUTBOX_BATCH_SIZE: int = Field(
        default=100, description="Maximum events published per outbox cycle."
    )
    OUTBOX_RETENTION_SECONDS: int = Field(
        default=86400, description="How long delivered outbox rows are kept for auditing."
    )

    # --- Redis (redis-service) --- #
    REDIS_HOST: str = Field(default="localhost", description="Redis host.")
    REDIS_PORT: int = Field(default=6379, description="Redis port.")
    REDIS_DB: int = Field(default=0, description="Redis logical database index.")
    REDIS_USERNAME: str | None = Field(default=None, description="Redis ACL username, if any.")
    REDIS_PASSWORD: str | None = Field(
        default=None, description="Redis password (inline fallback)."
    )
    REDIS_PASSWORD_FILE: str | None = Field(
        default=None, description="Path to a file holding the Redis password."
    )
    REDIS_URL: str | None = Field(default=None, description="Explicit Redis URL override.")

    # --- RabbitMQ (rabbitmq-service) --- #
    RABBITMQ_HOST: str = Field(default="localhost", description="RabbitMQ host.")
    RABBITMQ_PORT: int = Field(default=5672, description="RabbitMQ AMQP port.")
    RABBITMQ_VHOST: str = Field(default="/", description="RabbitMQ virtual host.")
    RABBITMQ_USER: str = Field(default="guest", description="RabbitMQ user (inline fallback).")
    RABBITMQ_USER_FILE: str | None = Field(
        default=str(SECRETS_DIR / "rabbitmq" / "rabbitmq_user"),
        description="Path to a file holding the RabbitMQ user.",
    )
    RABBITMQ_PASSWORD: str = Field(
        default="guest", description="RabbitMQ password (inline fallback)."
    )
    RABBITMQ_PASSWORD_FILE: str | None = Field(
        default=str(SECRETS_DIR / "rabbitmq" / "rabbitmq_password"),
        description="Path to a file holding the RabbitMQ password.",
    )
    RABBITMQ_URL: str | None = Field(default=None, description="Explicit RabbitMQ URL override.")

    @property
    def redis_password(self) -> str | None:
        """Redis password, read from REDIS_PASSWORD_FILE when configured."""
        return resolve_secret(self.REDIS_PASSWORD, self.REDIS_PASSWORD_FILE)

    @property
    def redis_url(self) -> str:
        """Build the Redis connection URL from the individual settings."""
        if self.REDIS_URL:
            return self.REDIS_URL

        password = self.redis_password
        credentials = ""
        if password:
            username = quote(self.REDIS_USERNAME or "", safe="")
            credentials = f"{username}:{quote(password, safe='')}@"

        return f"redis://{credentials}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def rabbitmq_user(self) -> str:
        """RabbitMQ user, read from RABBITMQ_USER_FILE when configured."""
        return resolve_secret(self.RABBITMQ_USER, self.RABBITMQ_USER_FILE) or self.RABBITMQ_USER

    @property
    def rabbitmq_password(self) -> str:
        """RabbitMQ password, read from RABBITMQ_PASSWORD_FILE when configured."""
        return (
            resolve_secret(self.RABBITMQ_PASSWORD, self.RABBITMQ_PASSWORD_FILE)
            or self.RABBITMQ_PASSWORD
        )

    @property
    def rabbitmq_url(self) -> str:
        """Build the AMQP connection URL from the individual settings."""
        if self.RABBITMQ_URL:
            return self.RABBITMQ_URL

        user = quote(self.rabbitmq_user, safe="")
        password = quote(self.rabbitmq_password, safe="")

        # aio-pika expects the default vhost "/" as a double trailing slash.
        vhost = self.RABBITMQ_VHOST
        path = "//" if vhost == "/" else f"/{quote(vhost, safe='')}"

        return f"amqp://{user}:{password}@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}{path}"


@lru_cache
def get_shared_settings():
    return SharedSettings()
