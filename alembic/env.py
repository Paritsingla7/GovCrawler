import logging
import os
import sys
from logging.config import fileConfig

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alembic import context
from sqlalchemy import engine_from_config, pool

from cloud.db import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically — but only when nothing has configured
# logging yet (bare `alembic` CLI usage). At runtime, `run_migrations()` is
# called from `Database.__init__()` on every server start, after
# `portal.main` has already attached the app's own FileHandler to the root
# logger; fileConfig()'s default disable_existing_loggers=True would silently
# rip that handler off root and disable every already-created `portal.*`
# logger, so skip it whenever the root logger is already configured.
if config.config_file_name is not None and not logging.getLogger().hasHandlers():
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The bare `alembic upgrade head` CLI (deploy/docker-compose.yml's one-shot
# `migrate` service) has no app config to read from — point it at Postgres
# via env var instead of the SQLite URL baked into alembic.ini.
if os.environ.get("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])


# other values from the config, defined by the needs of env.py,
# can be acquired here.
# my_important_variable = config.get_main_option("my_important_variable")


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
