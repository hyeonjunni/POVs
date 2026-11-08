"""Synthetic B4 contracts through the real main app and outer SecureBoundary."""
import asyncio
import secrets
import tempfile
import logging
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy import event

from test_secure_catalogue import catalogue
from test_secure_identity import identity_client, create_room

from base64 import b64decode
PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOoCBABAAIgAN3LGpnNAAAAAElFTkSuQmCC")
MP4 = b"\x00\x00\x00\x18ftypmp42synthetic-private-video"


@pytest.fixture
def media_client(identity_client):
    c = identity_client
    c.session, _, _ = c.signup()
    c.room, _ = create_room(c, c.session)
    c.auth = {"Authorization": "Bearer " + c.session["access_token"]}
    return c


def record(c, kind="image"):
    response = c.request("POST", "/uploads", headers=c.auth, json=dict(room_id=c.room["room_id"], type=kind,
        title="PRIVATE synthetic title", description="PRIVATE description", tags=[" memory ", "MEMORY"]))
    assert response.status_code == 200, response.text
    return response.json()


def upload(c, row=None, content=PNG, mime="image/png"):
    row = row or record(c)
    response = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth,
        files={"file": ("PRIVATE-name" + {"image/png": ".png", "video/mp4": ".mp4", "audio/wav": ".wav"}[mime], content, mime)})
    assert response.status_code == 200, response.text
    return response.json()


def content_path(row):
    url = urlsplit(row["file_url"])
    return url.path.removeprefix("/api/v1") + "?" + url.query


def job(c, row):
    response = c.request("POST", "/records/" + row["upload_id"] + "/processing-jobs", headers=c.auth,
        json=dict(upload_file_id=row["file_id"], processor="ocr"))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("content,mime,kind", [(PNG, "image/png", "image"), (MP4, "video/mp4", "video")])
def test_secure_upload_view_never_spools_and_keeps_metadata_encrypted(media_client, monkeypatch, content, mime, kind):
    c = media_client
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", lambda *a, **k: pytest.fail("plaintext spool"))
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", lambda *a, **k: pytest.fail("plaintext spool"))
    row = upload(c, record(c, kind), content, mime)
    response = c.request("GET", content_path(row), headers=c.auth)
    assert response.status_code == 200 and response.content == content
    assert response.headers["content-type"] == mime
    listed = c.request("GET", "/uploads/" + c.room["room_id"], headers=c.auth)
    assert listed.status_code == 200 and listed.json()[0]["file_id"] == row["file_id"]
    assert listed.json()[0]["title"] == "PRIVATE synthetic title"
    viewed = c.request("GET", content_path(listed.json()[0]), headers=c.auth)
    assert viewed.status_code == 200 and viewed.content == content
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT title FROM uploads")) is None
        f = db.execute(text("SELECT * FROM upload_files")).mappings().one()
        assert f["encrypted"] and all(f[k] is None for k in ("mime_type", "storage_path", "storage_bucket"))
        assert "PRIVATE" not in str(db.execute(text("SELECT * FROM secure_operations")).all())
    assert all(content not in blob for receipt, blob in c.objects.values())


@pytest.mark.parametrize("case", ["overlimit", "header_overflow", "forged_length", "extra_part", "incomplete", "wrong_magic"])
def test_secure_multipart_measured_limits_before_spool(media_client, monkeypatch, case):
    c = media_client
    row = record(c)
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", lambda *a, **k: pytest.fail("plaintext spool"))
    data = PNG if case != "overlimit" else PNG + b"x" * (20 * 1024 * 1024)
    if case == "wrong_magic":
        data = MP4
    header = b'Content-Disposition: form-data; name="file"; filename="x.png"\r\nContent-Type: image/png\r\n'
    if case == "header_overflow":
        header += b"X-Private: " + b"x" * 65536 + b"\r\n"
    body = b"--test\r\n" + header + b"\r\n" + data + b"\r\n--test--\r\n"
    if case == "extra_part":
        body = body.replace(b"--test--", b'--test\r\nContent-Disposition: form-data; name="extra"\r\n\r\nx\r\n--test--')
    if case == "incomplete":
        body = body[:-12]
    headers = c.auth | {"Content-Type": "multipart/form-data; boundary=test"}
    if case == "forged_length":
        headers["Content-Length"] = "1"
    response = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=headers, content=body)
    assert response.status_code == (413 if case in ("overlimit", "header_overflow") else 400), response.text
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM upload_files")) == 0


def test_replacement_preserves_file_id_invalidates_result_and_original(media_client):
    c = media_client
    row = upload(c)
    queued = job(c, row)
    replacement = upload(c, row, PNG + b"replacement")
    assert replacement["file_id"] == row["file_id"]
    assert c.request("GET", content_path(replacement), headers=c.auth).content == PNG + b"replacement"
    current = c.request("GET", "/processing-jobs/" + queued["job_id"], headers=c.auth)
    assert current.status_code == 200 and current.json()["result"] is None
    assert current.json()["status"] == "FAILED" and current.json()["failure_code"] == "source_changed"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM upload_files")) == 1
        assert db.scalar(text("SELECT source_revision FROM protected_pointers WHERE owner_type='file'")) >= 2
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE artifact_kind='original' AND state='RETIRED'")) == 1


def test_source_tamper_never_releases_plaintext(media_client):
    c = media_client
    row = upload(c)
    with c.engine.connect() as db:
        oid = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='file'"))
    receipt, blob = c.objects[oid]
    c.objects[oid] = receipt, blob[:-1] + bytes([blob[-1] ^ 1])
    response = c.request("GET", content_path(row), headers=c.auth)
    assert response.status_code == 503 and PNG not in response.content


def test_cross_room_membership_denies_media_and_processing(media_client):
    c = media_client
    row = upload(c)
    queued = job(c, row)
    other, _, _ = c.signup("other-user", "other@example.com")
    wrong = {"Authorization": "Bearer " + other["access_token"]}
    for method, path in [("GET", "/uploads/" + c.room["room_id"]), ("GET", content_path(row)),
        ("GET", "/processing-jobs/" + queued["job_id"]), ("POST", "/processing-jobs/" + queued["job_id"] + "/run"),
        ("POST", "/processing-jobs/" + queued["job_id"] + "/retry")]:
        response = c.request(method, path, headers=wrong)
        assert response.status_code in (404, 503) and "PRIVATE" not in response.text


def test_processing_secure_failure_never_persists_or_logs_private_provider_error(media_client, caplog):
    c = media_client
    row = upload(c)
    queued = job(c, row)
    # No configured provider is a safe typed failure, never a command fallback.
    response = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/run", headers=c.auth)
    assert response.status_code == 200 and response.json()["status"] == "FAILED"
    assert response.json()["failure_code"] == "processor_not_configured"
    retry = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/retry", headers=c.auth)
    assert retry.status_code == 200 and retry.json()["attempt"] == 1 and retry.json()["status"] == "PENDING"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT result_json FROM processing_jobs")) is None
        assert db.scalar(text("SELECT failure_message FROM processing_jobs")) is None
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("processor", ["ocr", "stt"])
def test_synthetic_processing_encrypted_result_provenance_and_replacement(media_client, processor):
    c = media_client
    row = upload(c) if processor == "ocr" else upload(c, record(c, "video"), MP4, "video/mp4")
    queued = c.request("POST", "/records/" + row["upload_id"] + "/processing-jobs", headers=c.auth,
        json=dict(upload_file_id=row["file_id"], processor=processor))
    assert queued.status_code == 200, queued.text
    from app.processing.synthetic import SyntheticOCRProvider, SyntheticSTTProvider
    c.app.state.secure_runtime.processing_providers = {"ocr": SyntheticOCRProvider(), "stt": SyntheticSTTProvider()}
    path = "/processing-jobs/" + queued.json()["job_id"]
    completed = c.request("POST", path + "/run", headers=c.auth)
    assert completed.status_code == 200 and completed.json()["status"] == "SUCCEEDED", completed.text
    result = completed.json()["result"]
    assert result["text"] == "Synthetic " + processor.upper()
    assert result["extractor_id"] == "synthetic-" + processor + "-v1"
    assert result["user_confirmed"] is False and result["review_status"] == "unreviewed"
    assert c.request("GET", path, headers=c.auth).json()["result"] == result
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT result_json FROM processing_jobs")) is None
        assert db.scalar(text("SELECT failure_message FROM processing_jobs")) is None
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE artifact_kind='processing_result' AND state='ACTIVE'")) == 1
    upload(c, row, PNG + b"new" if processor == "ocr" else MP4 + b"new", "image/png" if processor == "ocr" else "video/mp4")
    assert c.request("GET", path, headers=c.auth).json()["result"] is None


@pytest.mark.parametrize("race", ["source", "attempt", "membership", "cancel", "private_error"])
def test_provider_attempt_source_acl_and_cancellation_fences(media_client, monkeypatch, caplog, race):
    c = media_client
    row, queued = None, None
    row = upload(c)
    queued = job(c, row)
    from app.processing.synthetic import SyntheticOCRProvider
    from app.models.secure_catalogue import catalogue_mutation
    original = SyntheticOCRProvider.extract
    async def changed(self, content, mime):
        assert content == PNG and mime == "image/png"
        if race == "cancel":
            raise asyncio.CancelledError()
        if race == "private_error":
            raise ValueError("PRIVATE raw provider error")
        with c.engine.begin() as db:
            if race == "source":
                db.execute(text("UPDATE protected_pointers SET source_revision=source_revision+1,revision=revision+1 WHERE owner_type='file'"))
            elif race == "attempt":
                db.execute(text("UPDATE processing_jobs SET attempt=attempt+1,status='PENDING'"))
            else:
                mid = db.scalar(text("SELECT id FROM family_members WHERE room_id=:r"), dict(r=c.room["room_id"]))
                with catalogue_mutation(db, "membership", "family_members", [mid]):
                    db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room'"))
                    db.execute(text("UPDATE family_members SET is_active=false WHERE id=:m"), dict(m=mid))
        return await original(self, content, mime)
    monkeypatch.setattr(SyntheticOCRProvider, "extract", changed)
    c.app.state.secure_runtime.processing_providers = {"ocr": SyntheticOCRProvider()}
    response = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/run", headers=c.auth)
    if race == "membership":
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "RESOURCE_NOT_AVAILABLE"}}
    else:
        assert response.status_code in (200, 409, 503)
    assert response.json().get("result") is None and "PRIVATE" not in response.text and "PRIVATE" not in caplog.text
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE artifact_kind='processing_result' AND state='ACTIVE'")) == 0
        assert db.scalar(text("SELECT result_json FROM processing_jobs")) is None
        assert db.scalar(text("SELECT failure_message FROM processing_jobs")) is None
        if race == "membership":
            assert db.scalar(text("SELECT count(*) FROM family_members WHERE room_id=:r AND is_active=true"), dict(r=c.room["room_id"])) == 0


@pytest.mark.parametrize("fault", [None, "proof", "source", "duplicate", "second_cas"])
def test_batch_same_room_publishes_atomically_before_scope_advance(media_client, monkeypatch, fault):
    from app.api.routes import secure_media as media
    from app.services import secure_catalogue as cat
    from app.services.record_policy import register_legacy_record
    c = media_client
    staged = []
    for _ in range(2):
        oid = str(uuid4())
        with c.engine.begin() as db:
            db.execute(text("INSERT INTO uploads (id,room_id,uploader_user_id,type,status) VALUES (:o,:r,:u,'text','uploaded')"),
                dict(o=oid, r=c.room["room_id"], u=c.session["user"]["user_id"]))
            register_legacy_record(db, oid)
            media.pointer(db, "record", oid, c.room["room_id"])
        staged.append(media.stage(c.app.state.secure_runtime, c.session["user"]["user_id"], ("record", UUID(oid)), b"{}"))
    assert hasattr(cat, "publish_many"), "B4 requires atomic same-room publication"
    if fault == "proof":
        staged[1] = (*staged[1][:-1], cat.ReadbackProof(bytes(32)))
    if fault == "source":
        with c.engine.begin() as db:
            db.execute(text("UPDATE protected_pointers SET source_revision=source_revision+1 WHERE owner_type='record' AND owner_id=:o"), dict(o=str(staged[1][0].pointers[0][1])))
    if fault == "duplicate":
        staged[1] = staged[0]
    if fault == "second_cas":
        original = cat._cas
        def fail_second(db, sql, **params):
            if sql.startswith("UPDATE protected_pointers SET object_id") and params["object"] == str(staged[1][1].object_id):
                raise cat.SecureStateChanged()
            return original(db, sql, **params)
        monkeypatch.setattr(cat, "_cas", fail_second)
    if fault:
        with pytest.raises(cat.SecureStateChanged):
            with c.engine.begin() as db:
                cat.publish_many(db, tuple(staged))
    else:
        with c.engine.begin() as db:
            cat.publish_many(db, tuple(staged))
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM protected_pointers WHERE owner_type='record' AND state='ENCRYPTED_ACTIVE'")) == (0 if fault else 2)
        states = db.execute(text("SELECT state FROM secure_operations WHERE owner_type='record'")).scalars().all()
        assert states == (["RECEIPTED"] * 2 if fault else ["COMMITTED"] * 2)


def test_frontend_voice_type_and_wav_upload_contract(media_client):
    c = media_client
    raw = b"RIFF\x00\x00\x00\x00WAVEsynthetic audio"
    row = upload(c, record(c, "voice"), raw, "audio/wav")
    assert c.request("GET", content_path(row), headers=c.auth).content == raw


def test_multipart_parser_never_logs_private_wire_bytes(media_client, caplog, monkeypatch):
    c = media_client
    row = record(c)
    caplog.set_level(logging.WARNING, logger="multipart.multipart")
    monkeypatch.setattr(logging.getLogger("multipart.multipart"), "disabled", False)
    body = b'--test\r\nPrivate1: PRIVATE\r\nContent-Disposition: form-data; name="file"; filename="x"\r\nContent-Type: image/png\r\n\r\n' + PNG + b'\r\n--test--\r\nPRIVATE-TAIL'
    c.request("POST", "/uploads/" + row["upload_id"] + "/binary", content=body,
        headers=c.auth | {"Content-Type": "multipart/form-data; boundary=test"})
    assert not [r for r in caplog.records if r.name.startswith("multipart")], "Wire bytes must never enter parser diagnostics"


@pytest.mark.parametrize("processor", ["ocr", "stt"])
def test_command_provider_instance_is_rejected_before_any_execution(media_client, monkeypatch, processor):
    c = media_client
    row = upload(c) if processor == "ocr" else upload(c, record(c, "video"), MP4, "video/mp4")
    from app.processing.local_ocr import LocalTesseractOCRProvider
    from app.processing.local_stt import LocalWhisperSTTProvider
    provider = LocalTesseractOCRProvider() if processor == "ocr" else LocalWhisperSTTProvider()
    method = "extract" if processor == "ocr" else "transcribe"
    monkeypatch.setattr(provider, method, lambda *a, **k: pytest.fail("command provider executed"))
    c.app.state.secure_runtime.processing_providers = {processor: provider}
    queued = c.request("POST", "/records/" + row["upload_id"] + "/processing-jobs", headers=c.auth,
        json=dict(upload_file_id=row["file_id"], processor=processor)).json()
    result = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/run", headers=c.auth)
    assert result.status_code == 200 and result.json()["failure_code"] == "provider_forbidden"


def raw_scope(c, method, path, extra=()):
    url = urlsplit(path)
    full_path = url.path if url.path.startswith("/api/v1/") else "/api/v1" + url.path
    return dict(type="http", asgi={"version": "3.0"}, http_version="1.1", method=method,
        scheme="https", path=full_path, raw_path=full_path.encode(), query_string=url.query.encode(),
        headers=[(b"host", b"synthetic.local"), (b"authorization", c.auth["Authorization"].encode()), *extra],
        client=("127.0.0.1", 1), server=("synthetic.local", 443), root_path="")


@pytest.mark.parametrize("action", ["replace_race", "cancel_receive"])
def test_actual_outer_upload_receive_is_detached_and_cancel_safe(media_client, action):
    c = media_client
    row = upload(c)
    checked = set()
    event.listen(c.engine, "checkout", lambda connection, record, proxy: checked.add(id(connection)))
    event.listen(c.engine, "checkin", lambda connection, record: checked.discard(id(connection)))
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        count, messages = 0, []
        async def receive():
            nonlocal count
            assert not checked and c.app.state.secure_runtime.lock.held
            count += 1
            if count == 1:
                return dict(type="http.request", more_body=True, body=b'--test\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n' + PNG)
            if count == 2:
                entered.set()
                await release.wait()
                return dict(type="http.request", more_body=False, body=b"replacement\r\n--test--\r\n")
            await asyncio.Future()
        async def send(message):
            assert not checked and c.app.state.secure_runtime.lock.held
            messages.append(message)
        task = asyncio.create_task(c.app(raw_scope(c, "POST", "/uploads/" + row["upload_id"] + "/binary",
            [(b"content-type", b"multipart/form-data; boundary=test")]), receive, send))
        await asyncio.wait_for(entered.wait(), 2)
        if action == "replace_race":
            with c.engine.begin() as db:
                db.execute(text("UPDATE protected_pointers SET source_revision=source_revision+1,revision=revision+1 WHERE owner_type='file'"))
            release.set()
            await task
            assert messages[0]["status"] == 503
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert messages == []
        assert not c.app.state.secure_runtime.lock.held
    asyncio.run(run())
    assert c.request("GET", content_path(row), headers=c.auth).content == PNG


def test_outer_media_response_guard_survives_transport_cancel_until_sender_stops(media_client):
    c = media_client
    row = upload(c)
    async def run():
        entered, finishing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def receive():
            await asyncio.Future()
        async def send(message):
            assert c.app.state.secure_runtime.lock.held
            if message["type"] == "http.response.body":
                assert message["body"] == PNG
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    finishing.set()
                    await release.wait()
        task = asyncio.create_task(c.app(raw_scope(c, "GET", content_path(row)), receive, send))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(finishing.wait(), 2)
        assert c.app.state.secure_runtime.lock.held
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not c.app.state.secure_runtime.lock.held
    asyncio.run(run())


def test_outer_processing_cancel_waits_for_provider_termination_and_can_retry(media_client, monkeypatch):
    c = media_client
    row = upload(c)
    queued = job(c, row)
    from app.processing.synthetic import SyntheticOCRProvider
    checked = set()
    event.listen(c.engine, "checkout", lambda connection, record, proxy: checked.add(id(connection)))
    event.listen(c.engine, "checkin", lambda connection, record: checked.discard(id(connection)))
    async def run():
        entered, finishing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def provider(self, content, mime):
            assert not checked and content == PNG
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                finishing.set()
                await release.wait()
                raise
        monkeypatch.setattr(SyntheticOCRProvider, "extract", provider)
        c.app.state.secure_runtime.processing_providers = {"ocr": SyntheticOCRProvider()}
        async def receive():
            await asyncio.Future()
        async def send(message):
            pytest.fail("cancelled processing request sent a response")
        task = asyncio.create_task(c.app(raw_scope(c, "POST", "/processing-jobs/" + queued["job_id"] + "/run"), receive, send))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(finishing.wait(), 2)
        task.cancel()
        assert c.app.state.secure_runtime.lock.held
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not c.app.state.secure_runtime.lock.held
    asyncio.run(run())
    response = c.request("POST", "/processing-jobs/" + queued["job_id"] + "/retry", headers=c.auth)
    assert response.status_code == 200 and response.json()["status"] == "PENDING"


def test_file_storage_race_cannot_adopt_a_newer_metadata_pointer(media_client, monkeypatch):
    c = media_client
    row = upload(c)
    store = c.app.state.secure_runtime.store
    original = store.create
    changed = False
    def race(candidate, blob):
        nonlocal changed
        receipt = original(candidate, blob)
        if not changed:
            changed = True
            with c.engine.begin() as db:
                db.execute(text("UPDATE protected_pointers SET source_revision=source_revision+1,revision=revision+1 WHERE owner_type='record'"))
        return receipt
    monkeypatch.setattr(store, "create", race)
    response = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth,
        files={"file": ("x.png", PNG + b"replacement", "image/png")})
    assert response.status_code == 503
    assert c.request("GET", content_path(row), headers=c.auth).content == PNG


@pytest.mark.parametrize("lost", [False, True])
def test_created_original_without_durable_receipt_stays_quarantined(media_client, monkeypatch, lost):
    c = media_client
    row = upload(c)
    store = c.app.state.secure_runtime.store
    original = store.create
    def receipt_failure(candidate, blob):
        receipt = original(candidate, blob)
        if lost:
            raise RuntimeError("PRIVATE lost receipt")
        with c.engine.begin() as db:
            db.execute(text("UPDATE protected_pointers SET source_revision=source_revision+1,revision=revision+1 WHERE owner_type='file'"))
        return receipt
    monkeypatch.setattr(store, "create", receipt_failure)
    response = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth,
        files={"file": ("x.png", PNG + b"replacement", "image/png")})
    assert response.status_code == 503 and "PRIVATE" not in response.text
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='ORPHAN_UNRESOLVED'")) == 1
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state IN ('CLAIMED','RECEIPTED','VERIFIED')")) == 0
    assert c.request("GET", content_path(row), headers=c.auth).content == PNG


def test_exact_twenty_mib_and_forged_overlimit_stream_use_measured_bytes(media_client, monkeypatch):
    c = media_client
    row = record(c)
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", lambda *a, **k: pytest.fail("plaintext spool"))
    content = PNG + b"x" * (20 * 1024 * 1024 - len(PNG))
    row = upload(c, row, content)
    response = c.request("GET", content_path(row), headers=c.auth)
    assert response.status_code == 200 and response.content == content
    body = b'--test\r\nContent-Disposition: form-data; name="file"; filename="x"\r\nContent-Type: image/png\r\n\r\n' + content + b'x\r\n--test--\r\n'
    oversized = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", content=body,
        headers=c.auth | {"Content-Type": "multipart/form-data; boundary=test", "Content-Length": "1"})
    assert oversized.status_code == 413


def test_secure_tags_preserve_legacy_first_occurrence_order(media_client):
    c = media_client
    response = c.request("POST", "/uploads", headers=c.auth, json=dict(room_id=c.room["room_id"], type="text",
        title="synthetic", tags=[" Alpha ", "b", "ALPHA", "c", "B"]))
    assert response.status_code == 200 and response.json()["tags"] == ["Alpha", "b", "c"]
