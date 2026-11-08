from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import Base  # noqa: E402
from app import models  # noqa: E402,F401 -- import registers every table on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from scripts.adopt_alembic import migration_engine, migration_transaction  # BACKEND_ROOT is on sys.path above

    connectable = migration_engine(config.get_main_option("sqlalchemy.url"))
    try:
        with connectable.connect() as connection, migration_transaction(connection):
            from scripts.adopt_alembic import recorded_revisions, verify_frontier
            recorded = recorded_revisions(connection)
            if recorded is not None:
                failures = verify_frontier(connection, recorded)
                if failures:
                    raise RuntimeError("migration preflight refused: " + "; ".join(failures))
            opted_in = config.attributes.get("secure_quota_upgrade") or context.get_x_argument(as_dictionary=True).get("secure_quota_upgrade") == "1"
            if opted_in:
                from scripts.adopt_alembic import bridge_secure_quota_upgrade
                bridge_secure_quota_upgrade(connection, context.get_revision_argument())
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
