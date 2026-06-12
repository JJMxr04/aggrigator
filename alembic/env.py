"""Alembic env — uses the sync DSN so we don't need an async runner here."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from aggrigator.config import get_settings
from aggrigator.models import Base  # noqa: F401  — registers all models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate's hands off tables alembic doesn't own.

    Procrastinate manages its own schema (procrastinate.schema_manager) —
    without this filter, ``alembic revision --autogenerate`` emits
    drop_table for the ENTIRE job queue, and ``alembic check`` is too
    noisy to be useful.
    """
    if type_ == "table" and name.startswith("procrastinate_"):
        return False
    # Indexes/constraints attached to those tables arrive as their own
    # objects with a table backref.
    if type_ in ("index", "unique_constraint", "foreign_key_constraint"):
        table = getattr(obj, "table", None)
        if table is not None and table.name.startswith("procrastinate_"):
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
