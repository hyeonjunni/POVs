"""Join deployed album and family evidence lineages without rewriting either."""
from alembic import op

revision = "0010_merge_album_family"
down_revision = ("0007_video_albums_reads", "0009_ocr_review")
branch_labels = None
depends_on = None


def upgrade():
    from scripts.adopt_alembic import verify_frontier
    failures = verify_frontier(op.get_bind(), down_revision)
    if failures:
        raise RuntimeError("merge refused: " + "; ".join(failures))


def downgrade():
    # Only split the revision frontier; each branch retains its data-loss guards.
    pass
