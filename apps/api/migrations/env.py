"""Alembic environment for the business PostgreSQL database."""

from logging.config import fileConfig

from alembic import context
from cmp_api import (
    agent_registry,  # noqa: F401  # Register Agent tables in metadata.
    approvals,  # noqa: F401  # Register Plan approval tables in metadata.
    connections,  # noqa: F401  # Register environment connection tables (ticket 23).
    migration_events,  # noqa: F401  # Register event store/log tables (ticket 24).
    plan_inputs,  # noqa: F401  # Register Planning input tables (ticket 18).
    scope_selection,  # noqa: F401  # Register scope analysis tables (ticket 56).
    tunnels,  # noqa: F401  # Register tunnel exchange tables (ticket 16).
)
from cmp_api.config import settings
from cmp_api.db import Base
from sqlalchemy import engine_from_config, pool

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}), poolclass=pool.NullPool
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
