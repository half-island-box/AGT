"""Alembic migration environment — wired to our SQLAlchemy models."""

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Importing here triggers Base.metadata registration.
from agt.server.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """Resolve the database URL for migrations.

    Order of precedence:
    1. ``AGT_DB_PATH`` environment variable (absolute filesystem path).
    2. ``sqlalchemy.url`` from ``migrations.ini``.

    This lets the same ``migrations.ini`` work across machines whose home
    directory differs, without editing the checked-in file.
    """
    env_path = os.environ.get("AGT_DB_PATH")
    if env_path:
        return f"sqlite:///{Path(env_path).resolve()}"
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()