"""SECURE room ciphertext reservations and empty plaintext extension fences.

Existing SECURE 0009 catalogues require explicit, closed/drained upgrade:
python scripts/adopt_alembic.py --database-url URL --target-revision 0012_secure_room_storage_quota
The environment bridge installs only empty frozen album schemas transactionally.
Downgrade refuses SECURE mode or any retained reservation metadata.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_secure_room_storage_quota"
down_revision = "0011_legacy_room_storage_quota"
branch_labels = None
depends_on = None

PLAINTEXT_TABLES = ("family_people", "family_relationships", "albums", "album_photos",
                    "photo_upload_batches", "photo_upload_items", "album_videos", "record_read_receipts")
RESERVATION_CHECK = "reserved_ciphertext_bytes IS NULL OR (reserved_ciphertext_bytes > 0 AND kind='PAYLOAD' AND domain='room')"


def operation_triggers():
    from scripts.adopt_alembic import _secure_migration
    statements = _secure_migration().trigger_statements()
    old = "(OLD.kind='PAYLOAD' AND ((OLD.state='CLAIMED' AND NEW.state IN"
    new = ("(OLD.kind='PAYLOAD' AND ((OLD.state='CLAIMED' AND NEW.state='CLAIMED' "
           "AND OLD.reserved_ciphertext_bytes IS NULL AND NEW.reserved_ciphertext_bytes IS NOT NULL AND NEW.reserved_ciphertext_bytes > 0) "
           "OR (OLD.state='CLAIMED' AND NEW.state IN")
    reserve = ("OLD.kind='PAYLOAD' AND OLD.domain='room' AND OLD.state='CLAIMED' "
               "AND NEW.state='CLAIMED' AND OLD.reserved_ciphertext_bytes IS NULL "
               "AND NEW.reserved_ciphertext_bytes IS NOT NULL AND NEW.reserved_ciphertext_bytes > 0")
    result = []
    for sql in statements:
        if sql.startswith("CREATE TRIGGER m4_operation_transition "):
            sql = sql.replace(old, new)
            sql = sql.replace(" WHEN (", " WHEN ((NEW.reserved_ciphertext_bytes IS NOT OLD.reserved_ciphertext_bytes AND NOT (" + reserve + ")) OR ", 1)
        result.append(sql)
    result.append("CREATE TRIGGER m4_operation_quota_insert BEFORE INSERT ON secure_operations "
                  "WHEN NEW.reserved_ciphertext_bytes IS NOT NULL "
                  "BEGIN SELECT RAISE(ABORT,'reservation requires claimed operation'); END")
    return result


def album_triggers():
    from scripts.adopt_alembic import _home_migration
    populated = " OR ".join(f"EXISTS (SELECT 1 FROM {t})" for t in PLAINTEXT_TABLES)
    return [("CREATE TRIGGER album_legacy_cutover_fence BEFORE UPDATE ON security_control "
             "WHEN NEW.mode <> 'LEGACY' AND (" + populated + ") "
             "BEGIN SELECT RAISE(ABORT,'plaintext extension is not empty'); END")
            if sql.startswith("CREATE TRIGGER album_legacy_cutover_fence ")
            else sql for sql in _home_migration().trigger_statements()]


def graph_triggers():
    return [f"CREATE TRIGGER quota_{table}_{event.lower()} BEFORE {event} ON {table} "
            "WHEN COALESCE((SELECT mode FROM security_control WHERE id=1),'UNKNOWN') <> 'LEGACY' "
            "BEGIN SELECT RAISE(ABORT,'family graph requires LEGACY'); END"
            for table in ("family_people", "family_relationships")
            for event in ("INSERT", "UPDATE", "DELETE")]


def upgrade():
    bind = op.get_bind()
    from scripts.adopt_alembic import require_secure_quota_drain
    control = bind.execute(sa.text("SELECT mode FROM security_control WHERE id=1")).scalar_one()
    if control != "LEGACY":
        require_secure_quota_drain(bind)
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE secure_operations ADD COLUMN reserved_ciphertext_bytes BIGINT "
                   "CONSTRAINT ck_secure_operation_reserved_bytes CHECK (" + RESERVATION_CHECK + ")")
        for sql in operation_triggers():
            if sql.startswith("CREATE TRIGGER m4_operation_transition "):
                op.execute("DROP TRIGGER m4_operation_transition")
                op.execute(sql)
            elif sql.startswith("CREATE TRIGGER m4_operation_quota_insert "):
                op.execute(sql)
        op.execute("DROP TRIGGER album_legacy_cutover_fence")
        op.execute(album_triggers()[0])
        for sql in graph_triggers():
            op.execute(sql)
    else:
        op.add_column("secure_operations", sa.Column("reserved_ciphertext_bytes", sa.BigInteger(), nullable=True))
        op.create_check_constraint("ck_secure_operation_reserved_bytes", "secure_operations", RESERVATION_CHECK)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT mode,admission,runtime_generation FROM security_control")).all() != [("LEGACY", "CLOSED", None)]:
        raise RuntimeError("0012 downgrade refused: catalogue must be closed LEGACY")
    if bind.execute(sa.text("SELECT 1 FROM secure_operations WHERE reserved_ciphertext_bytes IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("0012 downgrade refused: retained reservation metadata")
    if bind.dialect.name == "sqlite":
        from scripts.adopt_alembic import _secure_migration, _home_migration
        op.execute("DROP TRIGGER m4_operation_transition")
        op.execute("DROP TRIGGER m4_operation_quota_insert")
        for sql in _secure_migration().trigger_statements():
            if sql.startswith("CREATE TRIGGER m4_operation_transition "):
                op.execute(sql)
        op.execute("DROP TRIGGER album_legacy_cutover_fence")
        op.execute(_home_migration().trigger_statements()[0])
        for sql in graph_triggers():
            op.execute("DROP TRIGGER " + sql.split()[2])
    else:
        op.drop_constraint("ck_secure_operation_reserved_bytes", "secure_operations", type_="check")
    op.drop_column("secure_operations", "reserved_ciphertext_bytes")
