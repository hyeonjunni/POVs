"""Allow native text extraction jobs without relabeling them as OCR.

Revision ID: 0014_native_text
Revises: 0013_secure_reconciliation
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_native_text"
down_revision = "0013_secure_reconciliation"
branch_labels = None
depends_on = None


class NativeTextDowngradeError(RuntimeError):
    def __str__(self) -> str:
        return "refusing downgrade: native text jobs would be lost"


def _replace_processor_check(expression: str) -> None:
    """Preserve SQLite security fences across Alembic's table rebuild."""
    bind = op.get_bind()
    triggers = []
    if bind.dialect.name == "sqlite":
        triggers = bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='processing_jobs' ORDER BY name"
        ).scalars().all()
    with op.batch_alter_table("processing_jobs") as batch:
        batch.drop_constraint("ck_processing_job_processor_allowed", type_="check")
        batch.create_check_constraint("ck_processing_job_processor_allowed", expression)
    for sql in triggers:
        bind.exec_driver_sql(sql)


def upgrade() -> None:
    _replace_processor_check("processor IN ('ocr', 'stt', 'text')")


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM processing_jobs WHERE processor='text' LIMIT 1"
    )).first() is not None:
        raise NativeTextDowngradeError()
    _replace_processor_check("processor IN ('ocr', 'stt')")
