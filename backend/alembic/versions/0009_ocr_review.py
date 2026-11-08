"""Source-bound LEGACY OCR review metadata; no table rebuild."""
from alembic import op
import sqlalchemy as sa

revision = "0009_ocr_review"
down_revision = "0008_upload_file_uploader"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("processing_jobs", sa.Column("review_json", sa.Text(), nullable=True))


def downgrade():
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM processing_jobs WHERE review_json IS NOT NULL LIMIT 1"
    ).first() is not None:
        raise RuntimeError("refusing downgrade: OCR review metadata would be lost")
    op.drop_column("processing_jobs", "review_json")
