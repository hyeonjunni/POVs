"""Room guards must precede child writes in every legacy worker path."""
import pytest
from sqlalchemy import event
import test_retrieval_indexing as ri
from app.models.upload import UploadFile
from app.models.processing_job import ProcessingJob
from app.services.processing_jobs import review_ocr_result, mark_running, recover_abandoned_job

@pytest.mark.parametrize("operation", ["index", "review", "processing", "recovery"])
def test_room_guard_is_first_write(operation):
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            db.get(UploadFile, ri.FILE_ID).file_uploader_user_id = ri.USER_ID
            if operation == "review":
                db.get(ProcessingJob, ri.JOB_ID).review_json = None
            if operation == "processing":
                db.get(ProcessingJob, ri.JOB_ID).status = "PENDING"
            db.commit()
        writes = []
        def capture(conn, cursor, statement, parameters, context, many):
            if statement.lstrip().upper().startswith(("UPDATE", "DELETE", "INSERT")):
                writes.append(statement.lower())
        event.listen(engine, "before_cursor_execute", capture)
        if operation == "index":
            ri.index(tracker, ri.FakeEmbeddingProvider(tracker))
        else:
            with tracker.session_maker() as db:
                if operation == "review":
                    review_ocr_result(db, ri.JOB_ID, ri.USER_ID, "reject")
                elif operation == "processing":
                    mark_running(db, ri.JOB_ID)
                else:
                    recover_abandoned_job(db, ri.JOB_ID)
        assert writes and writes[0].startswith("update family_rooms"), writes
    finally:
        engine.dispose()


def test_secure_room_guards_are_sorted_and_do_not_advance_snapshot_revision():
    from uuid import UUID
    from sqlalchemy import create_engine, text
    from app.services.secure_catalogue import lock_rooms
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE security_scopes (domain TEXT, owner_id TEXT, revision INTEGER)"))
        rooms = [str(UUID(int=2)), str(UUID(int=1))]
        for room in rooms:
            db.execute(text("INSERT INTO security_scopes VALUES ('room',:r,7)"), {"r": room})
        writes = []
        def capture(conn, cursor, statement, parameters, context, many):
            if statement.startswith("UPDATE"):
                writes.append(parameters[0])
        event.listen(engine, "before_cursor_execute", capture)
        lock_rooms(db, [*rooms, rooms[0]])
        assert writes == sorted(rooms)
        assert db.execute(text("SELECT revision FROM security_scopes")).scalars().all() == [7, 7]
    engine.dispose()


from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client


def test_secure_upload_processing_index_review_take_room_guard_first(media_client, monkeypatch):
    from test_secure_retrieval import processed, index_secure
    from test_secure_ocr_review import review
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    first_writes = []
    def begin(conn):
        conn.info["lock_inventory_first_write"] = False
    def capture(conn, cursor, statement, parameters, context, many):
        sql = statement.strip().upper()
        if sql.startswith(("UPDATE", "INSERT", "DELETE")) and not conn.info.get("lock_inventory_first_write"):
            conn.info["lock_inventory_first_write"] = True
            first_writes.append(sql)
    event.listen(c.engine, "begin", begin)
    event.listen(c.engine, "before_cursor_execute", capture)
    try:
        row, done = processed(c, monkeypatch)
        assert index_secure(c, row, done).status_code == 200
        rejected_row, rejected_job = processed(c, monkeypatch, approved=False)
        assert review(c, rejected_job, "reject").status_code == 200
        assert len(first_writes) >= 8
        assert all(sql.startswith("UPDATE SECURITY_SCOPES SET REVISION=REVISION") for sql in first_writes), first_writes
    finally:
        event.remove(c.engine, "before_cursor_execute", capture)
        event.remove(c.engine, "begin", begin)


def test_recovery_of_missing_job_remains_a_noop():
    engine, tracker = ri.setup_indexing()
    try:
        with tracker.session_maker() as db:
            assert recover_abandoned_job(db, "missing") is False
    finally:
        engine.dispose()
