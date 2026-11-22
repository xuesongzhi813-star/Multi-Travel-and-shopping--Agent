"""Configuration and migration entry point for the business database."""

from dataclasses import dataclass, field
from hashlib import blake2b
import os
from pathlib import Path
import re

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateSchema


ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class DatabaseSettings:
    schema: str
    name: str
    user: str
    password: str = field(repr=False)
    port: int = 5432


def local_value(name: str, default: str = "") -> str:
    env_file = Path(os.environ.get("P2_ENV_FILE", ROOT / ".env"))
    return os.environ.get(name, dotenv_values(env_file, interpolate=False).get(name) or default)


def validate_schema(schema: str) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) is None:
        raise ValueError("invalid business schema")


def read_settings() -> DatabaseSettings:
    schema = local_value("P2_DB_SCHEMA", "travel_business")
    validate_schema(schema)
    password = local_value("POSTGRES_PASSWORD")
    if not password:
        raise ValueError("missing POSTGRES_PASSWORD")
    try:
        port = int(local_value("POSTGRES_PORT", "5432"))
    except ValueError:
        raise ValueError("invalid POSTGRES_PORT") from None
    if not 1 <= port <= 65535:
        raise ValueError("invalid POSTGRES_PORT")
    return DatabaseSettings(
        schema, local_value("POSTGRES_DB", "travel"), local_value("POSTGRES_USER", "travel"),
        password, port,
    )


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    validate_schema(settings.schema)
    return create_async_engine(
        URL.create(
            "postgresql+psycopg", username=settings.user, password=settings.password,
            host="127.0.0.1", port=settings.port, database=settings.name,
        ),
        pool_size=4,
        max_overflow=0,
        pool_timeout=5,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=8000 -c lock_timeout=5000"},
    )


async def migrate_business(engine: AsyncEngine, schema: str) -> None:
    validate_schema(schema)
    key = int.from_bytes(blake2b(("travel-business:" + schema).encode(), digest_size=8).digest(), "big", signed=True)

    def upgrade(connection):
        config = Config(ROOT / "alembic.ini")
        config.attributes.update(connection=connection, business_schema=schema)
        command.upgrade(config, "head")

    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        await connection.execute(CreateSchema(schema, if_not_exists=True))
        await connection.run_sync(upgrade)
