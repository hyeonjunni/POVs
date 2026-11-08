"""Shared pytest bootstrap for backend/tests.

Guarantees ``backend/`` is importable as the ``app``/``scripts`` package root
regardless of the working directory pytest was invoked from (conftest.py is
collected before any sibling test module, so this must stay side-effect-light
and import nothing from ``app`` at module scope).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def fk_enforced_engine(url: str = "sqlite://", **kwargs):
    """Every SQLite test engine goes through here so PRAGMA foreign_keys=ON
    is set on each new DBAPI connection (SQLite FKs are per-connection)."""
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
        if url == "sqlite://":
            kwargs.setdefault("poolclass", StaticPool)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


POSTGRES_SKIP_REASON = "PostgreSQL not configured — cross-database equivalence NOT proven"


def postgres_url_or_skip() -> str:
    url = os.environ.get("AMBIENT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip(POSTGRES_SKIP_REASON)
    return url


@pytest.fixture(autouse=True)
def migration_storage_inventory(tmp_path_factory, monkeypatch):
    """Keep every Alembic test isolated from the repository media directory."""
    root = tmp_path_factory.mktemp("legacy-storage")
    monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_BACKFILL_APPROVED", "1")
    monkeypatch.setenv("AMBIENT_LEGACY_STORAGE_ROOT", str(root.resolve()))
    yield
