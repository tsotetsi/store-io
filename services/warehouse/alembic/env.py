import asyncio
import os
import sys
from logging.config import fileConfig
from urllib.parse import quote

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Add the current working directory to Python path to find the app module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Import application settings and Base
from warehouse.app.config import get_warehouse_settings
from warehouse.app.database import Base
from warehouse.app.models import *  # noqa: F403 - Ensure all models are loaded

settings = get_warehouse_settings()


def _resolve_db_host() -> tuple[str, int]:
    """Resolve the DB host and port for the current environment.

    Inside Docker Compose the service name works. On the host we need
    localhost + the published port so Alembic can reach the container.
    """
    import socket

    host = settings.POSTGRES_HOST
    port = settings.POSTGRES_PORT

    # If the configured host isn't resolvable, assume we're on the host
    # and use localhost with the published port from .env.
    if host != "localhost":
        try:
            socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError:
            host = "localhost"
            # Read the published port from .env (Docker Compose variable).
            # pydantic-settings doesn't expose these, so load .env directly.
            try:
                from dotenv import dotenv_values

                env_vars = dotenv_values(
                    os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
                )
                published_port = env_vars.get("WAREHOUSE_DB_PORT")
                port = int(published_port) if published_port else settings.POSTGRES_PORT
            except Exception:
                port = settings.POSTGRES_PORT

    return host, port


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Resolve the DB host/port for the current environment (Docker vs host)
host, port = _resolve_db_host()

# Build a sync DB URL for Alembic using the resolved host and port
user = settings.database_user
password = settings.database_password
db_name = settings.database_name

db_url_sync = (
    f"postgresql+psycopg2://{quote(user, safe='')}:{quote(password, safe='')}"
    f"@{host}:{port}/{db_name}"
)

# Escape % for configparser interpolation syntax
config.set_main_option(
    "sqlalchemy.url",
    db_url_sync.replace("%", "%%"),
)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with a database connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in async mode using the application's engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = db_url_sync.replace(
        "postgresql+psycopg2", "postgresql+asyncpg"
    )

    connectable = create_async_engine(
        configuration["sqlalchemy.url"],
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
