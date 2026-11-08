"""Synthetic B2 runtime contracts, including real Linux process/descriptor checks."""
import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config
from test_secure_catalogue import catalogue, service_case, control_update, generation, put
from sqlalchemy import text


def runtime_api():
    assert importlib.util.find_spec("app.services.secure_runtime"), "B2 admission boundary is absent"
    from app.services import secure_runtime
    return secure_runtime


linux = pytest.mark.skipif(sys.platform != "linux", reason="Requires real Linux flock/process semantics")


@pytest.fixture
def lock_path(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "operation.lock"
    path.touch(mode=0o600)
    return path


@linux
def test_linux_two_process_exclusion_and_same_inode_reacquisition(lock_path):
    rt = runtime_api()
    lock = rt.LinuxLock(lock_path, approved_uid=os.geteuid())
    assert lock.try_acquire()
    inode = lock_path.stat().st_ino
    script = """
import os, sys
from app.services.secure_runtime import LinuxLock
lock = LinuxLock(sys.argv[1], approved_uid=os.geteuid())
held = lock.try_acquire()
print('acquired' if held else 'busy', flush=True)
if held: lock.release()
"""
    env = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    def contender():
        return subprocess.run([sys.executable, "-c", script, str(lock_path)], env=env,
                              capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    try:
        assert contender() == "busy"
    finally:
        lock.release()
    assert contender() == "acquired"
    assert lock_path.stat().st_ino == inode


@linux
def test_linux_killed_owner_releases_fd_but_never_grants_admission(lock_path):
    rt = runtime_api()
    script = """
import os, sys
from app.services.secure_runtime import LinuxLock
lock = LinuxLock(sys.argv[1], approved_uid=os.geteuid())
assert lock.try_acquire()
print('held', flush=True)
sys.stdin.read()
"""
    env = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "held"
        queued = rt.LinuxLock(lock_path, approved_uid=os.geteuid())
        assert not queued.try_acquire()
        process.kill()
        process.wait(timeout=5)
        assert queued.try_acquire()
        queued.release()
        # Acquiring the inode does not substitute for the persisted generation check.
        calls = []
        def closed():
            calls.append(True)
            raise rt.SecureUnavailable()
        runtime = rt.SecureRuntime(queued, check_admission=closed)
        async def attempt():
            with pytest.raises(rt.SecureUnavailable):
                async with runtime.operation_guard():
                    pytest.fail("dead owner's generation admitted")
        asyncio.run(attempt())
        assert calls == [True]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@linux
def test_linux_inherited_token_rejected_and_descriptor_cloexec(lock_path):
    rt = runtime_api()
    lock = rt.LinuxLock(lock_path, approved_uid=os.geteuid())
    assert lock.try_acquire()
    assert not os.get_inheritable(lock.fd)
    pid = os.fork()
    if pid == 0:
        try:
            lock.try_acquire()
        except rt.SecureUnavailable:
            os._exit(0)
        os._exit(9)
    try:
        assert os.waitpid(pid, 0)[1] == 0
    finally:
        lock.release()


def test_runtime_boundary_is_required():
    assert runtime_api().SecureRuntime


class FakeLock:
    def __init__(self):
        self.held = False
        self.releases = 0

    def try_acquire(self):
        if self.held:
            return False
        self.held = True
        return True

    def release(self):
        assert self.held
        self.held = False
        self.releases += 1


def test_nested_guard_reuses_token_and_rechecks_after_wait():
    rt = runtime_api()
    lock = FakeLock()
    calls = []
    runtime = rt.SecureRuntime(lock, check_admission=lambda: calls.append("checked"))
    async def run():
        async with runtime.operation_guard() as token:
            async with runtime.operation_guard(token) as nested:
                assert nested is token
                assert lock.held
        assert calls == ["checked"] and lock.releases == 1
        with pytest.raises(rt.SecureUnavailable):
            async with runtime.operation_guard(token):
                pass
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"database_url": "postgresql://localhost/catalogue"},
    {"use_cloud_sql_connector": True}, {"instance_connection_name": "synthetic:region:db"},
    {"use_gcs_media_storage": True}, {"gemma_endpoint_url": "http://synthetic.invalid"},
    {"retrieval_embedding_endpoint_url": "http://synthetic.invalid"},
    {"ocr_executable_path": "synthetic-ocr"}, {"stt_executable_path": "synthetic-stt"},
    {"jwt_secret_key": "ambient-legacy-dev-secret"}, {"jwt_secret_key": "short"},
])
def test_secure_config_rejects_conflicts_before_side_effects(change):
    rt = runtime_api()
    from app.core.config import Settings, validate_secure_config
    settings = Settings(_env_file=None, **(dict(security_mode="SECURE", database_url="sqlite:///synthetic.sqlite",
        use_gcs_media_storage=False, use_cloud_sql_connector=False, prefer_ollama_for_gemma=False,
        jwt_secret_key="synthetic-test-only-signing-key-00000000000") | change))
    with pytest.raises(rt.SecureUnavailable):
        validate_secure_config(settings)


def test_bounded_response_holds_guard_through_terminal_send():
    rt = runtime_api()
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None)
    sent = []
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"x" * 70000})
    async def send(message):
        assert lock.held
        sent.append(message)
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)(dict(type="http", headers=[]), receive, send))
    assert [len(m.get("body", b"")) for m in sent] == [0, 65536, 4464]
    assert sent[-1]["more_body"] is False
    assert lock.releases == 1


@pytest.mark.parametrize("body_size,header_size,status", [(4194304, 0, 200), (4194305, 0, 413), (1, 65537, 413)])
def test_complete_response_size_checked_before_start(body_size, header_size, status):
    rt = runtime_api()
    runtime = rt.SecureRuntime(FakeLock(), check_admission=lambda: None)
    sent = []
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x", b"a" * header_size)]})
        await send({"type": "http.response.body", "body": b"a" * body_size})
    async def send(message):
        sent.append(message)
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)(dict(type="http", headers=[]), receive, send))
    assert sent[0]["status"] == status
    assert sum(m["type"] == "http.response.start" for m in sent) == 1
    if status == 413:
        assert sum(len(m.get("body", b"")) for m in sent) < 100


@pytest.mark.parametrize("bad", ["missing", "extra", "stale", "control"])
def test_generation_verification_denies_uncertain_delivery(service_case, bad):
    rt = runtime_api()
    from app.storage.contracts import KeyRef
    from app.storage.key_vault import SystemdKeyVault
    case = service_case
    ref = KeyRef("room", case["room"], case["key"], 1)
    inv = rt.Invocation("synthetic.service", uuid4(), uuid4(), 1000)
    with case["engine"].begin() as db:
        control_update(db, admission="CLOSED")
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=str(ref.key_id), key_epoch=1,
            domain="room", owner_id=str(ref.owner_id))
        if bad == "stale":
            control_update(db, admission="CLOSED")
        if bad == "control":
            control_update(db, mode="QUARANTINED")
    class Vault:
        def load(self, supplied):
            assert supplied == ref
            return bytes(32)
    names = {SystemdKeyVault.credential_name(ref)}
    names = set() if bad == "missing" else names | {"extra"} if bad == "extra" else names
    with pytest.raises(rt.SecureUnavailable):
        rt.verify_generation(case["engine"], generation_id=UUID(gid), invocation=inv,
                             vault=Vault(), delivered_names=lambda: names)
    with case["engine"].connect() as db:
        assert db.scalar(text("SELECT admission FROM security_control")) == "CLOSED"
        assert db.scalar(text("SELECT state FROM credential_generations WHERE generation_id=:id"), dict(id=gid)) == "FAILED"


def test_startup_missing_control_and_legacy_mode_mismatch_deny(service_case):
    rt = runtime_api()
    from conftest import fk_enforced_engine
    empty = fk_enforced_engine()
    try:
        with pytest.raises(rt.SecureUnavailable):
            rt.validate_catalogue_mode(empty, "SECURE")
        with pytest.raises(rt.SecureUnavailable):
            rt.validate_catalogue_mode(service_case["engine"], "LEGACY")
    finally:
        empty.dispose()


def test_generation_load_has_no_open_database_connection(service_case):
    rt = runtime_api()
    from sqlalchemy import event
    from app.storage.contracts import KeyRef
    from app.storage.key_vault import SystemdKeyVault
    case = service_case
    ref = KeyRef("room", case["room"], case["key"], 1)
    inv = rt.Invocation("synthetic.service", uuid4(), uuid4(), 1000)
    with case["engine"].begin() as db:
        control_update(db, admission="CLOSED")
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=str(ref.key_id), key_epoch=1,
            domain="room", owner_id=str(ref.owner_id))
    checked_out = []
    event.listen(case["engine"], "checkout", lambda *args: checked_out.append(1))
    event.listen(case["engine"], "checkin", lambda *args: checked_out.pop())
    class Vault:
        def load(self, supplied):
            assert checked_out == []
            assert supplied == ref
            return bytes(32)
    rt.verify_generation(case["engine"], generation_id=UUID(gid), invocation=inv,
                         vault=Vault(), delivered_names=lambda: {SystemdKeyVault.credential_name(ref)})
    rt.check_generation(case["engine"], UUID(gid), inv)
    with pytest.raises(rt.SecureUnavailable):
        rt.check_generation(case["engine"], UUID(gid), rt.Invocation(inv.unit_name, uuid4(), inv.boot_id, inv.approved_uid))


def test_response_disconnect_stops_sender_before_unlock():
    rt = runtime_api()
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None)
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"protected"})
        async def send(message):
            if message["type"] == "http.response.body":
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    assert lock.held
                    stopped.set()
        async def receive():
            await started.wait()
            return {"type": "http.disconnect"}
        await rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send)
        assert stopped.is_set() and lock.releases == 1
    asyncio.run(run())


def test_total_deadline_applies_to_cumulative_immediate_chunks():
    rt = runtime_api()
    clock = [0.0]
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None, monotonic=lambda: clock[0])
    sent = []
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 200000})
    async def send(message):
        sent.append(message)
        clock[0] += 8  # cumulative virtual time; no wall-clock sleep
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
    assert len(sent) == 2  # start + first chunk; 16 seconds already elapsed
    assert lock.releases == 1


def test_asgi_secure_main_never_imports_plaintext_handlers(tmp_path):
    # A separate import avoids leaking secure settings into legacy regression tests.
    script = """
import asyncio, sys
from app.main import app
from app.services.secure_runtime import SecureRuntime
class Lock:
    def try_acquire(self): return True
    def release(self): pass
app.state.secure_runtime = SecureRuntime(Lock(), check_admission=lambda: None)
assert 'app.api.routes.uploads' not in sys.modules
async def run():
    sent=[]
    async def send(m): sent.append(m)
    async def receive(): await asyncio.Future()
    await app(dict(type='http',method='GET',path='/api/v1/uploads',raw_path=b'/api/v1/uploads',query_string=b'',headers=[],http_version='1.1',scheme='http',server=('test',80),client=('test',1)), receive, send)
    assert sent[0]['status']==503
    assert len(sent)==2
asyncio.run(run())
"""
    env = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "SECURITY_MODE": "SECURE", "DATABASE_URL": f"sqlite:///{tmp_path / 'empty.sqlite'}",
        "USE_GCS_MEDIA_STORAGE": "false", "USE_CLOUD_SQL_CONNECTOR": "false",
        "PREFER_OLLAMA_FOR_GEMMA": "false", "PREFER_OLLAMA_FOR_EXAONE": "false",
        "JWT_SECRET_KEY": "synthetic-test-only-signing-key-00000000000"}
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@linux
def test_linux_same_process_contender_waits_without_second_owner(lock_path):
    rt = runtime_api()
    lock = rt.LinuxLock(lock_path, approved_uid=os.geteuid())
    assert lock.try_acquire()
    try:
        assert lock.try_acquire() is False
    finally:
        lock.release()


def test_suppressed_sender_cancellation_failstops_without_release(monkeypatch):
    rt = runtime_api()
    lock = FakeLock()
    class KilledWorker(BaseException):
        pass
    stopped = []
    def kill():
        assert lock.held
        stopped.append(True)
        raise KilledWorker()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None, fail_stop=kill)
    async def run():
        blocked, finish = asyncio.Event(), asyncio.Event()
        tasks = []
        real_wait = asyncio.wait
        async def virtual_wait(waiting, *, timeout, **kwargs):
            if timeout > 1:
                tasks.extend(waiting)
                await blocked.wait()
                return set(), set(waiting)
            await asyncio.sleep(0)
            return {t for t in waiting if t.done()}, {t for t in waiting if not t.done()}
        monkeypatch.setattr(rt.asyncio, "wait", virtual_wait)
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"secret"})
        async def send(message):
            blocked.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
        async def receive():
            await asyncio.Future()
        try:
            with pytest.raises(KilledWorker):
                await rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send)
            assert stopped == [True] and lock.releases == 0
        finally:
            finish.set()
            for task in tasks:
                task.cancel()
            await real_wait(tasks, timeout=1)
    asyncio.run(run())


def test_disconnect_before_start_never_sends_protected_bytes():
    rt = runtime_api()
    runtime = rt.SecureRuntime(FakeLock(), check_admission=lambda: None)
    sent = []
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"secret"})
    async def send(message):
        sent.append(message)
    async def receive():
        return {"type": "http.disconnect"}
    asyncio.run(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
    assert sent == []


def test_repeated_request_cancellation_cannot_unlock_live_sender():
    rt = runtime_api()
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None)
    async def run():
        entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"secret"})
        async def send(message):
            if message["type"] == "http.response.body":
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish.wait()
                    assert lock.held
        async def receive():
            await asyncio.Future()
        request = asyncio.create_task(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
        await entered.wait()
        request.cancel()
        await cancelled.wait()
        request.cancel()
        await asyncio.sleep(0)
        try:
            assert lock.held and lock.releases == 0
        finally:
            finish.set()
            await asyncio.gather(request, return_exceptions=True)
        assert lock.releases == 1
    asyncio.run(run())


def test_queued_deletion_linearizes_after_terminal_send():
    rt = runtime_api()
    lock = FakeLock()
    deleted = []
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None)
    async def run():
        final, release = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"secret"})
        async def send(message):
            if message["type"] == "http.response.body":
                final.set()
                await release.wait()
                assert not deleted and lock.held
        async def receive():
            await asyncio.Future()
        async def delete():
            async with runtime.operation_guard():
                deleted.append(True)
        request = asyncio.create_task(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
        await final.wait()
        deletion = asyncio.create_task(delete())
        await asyncio.sleep(0)
        assert not deleted
        release.set()
        await asyncio.gather(request, deletion)
        assert deleted == [True] and lock.releases == 2
    asyncio.run(run())


@pytest.mark.parametrize("case", ["headers", "sse"])
def test_sensitive_headers_and_sse_never_reach_transport(case):
    rt = runtime_api()
    runtime = rt.SecureRuntime(FakeLock(), check_admission=lambda: None)
    sent = []
    async def app(scope, receive, send):
        headers = [(b"x-private", b"secret-header"), (b"content-type", b"application/json")]
        if case == "sse":
            headers = [(b"content-type", b"text/event-stream")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"{}"})
    async def send(message):
        sent.append(message)
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
    assert b"secret-header" not in repr(sent).encode()
    if case == "sse":
        assert sent[0]["status"] == 503


def test_database_config_gate_runs_before_credentials_or_connector(monkeypatch):
    rt = runtime_api()
    from app.core import database
    from app.core.config import Settings
    configured = Settings(_env_file=None, security_mode="SECURE", database_url="postgresql://localhost/db",
        instance_connection_name="synthetic:region:db", db_user="synthetic", db_name="synthetic")
    monkeypatch.setattr(database, "settings", configured)
    calls = []
    monkeypatch.setattr(database, "_apply_google_credentials_env", lambda: calls.append("credentials"))
    monkeypatch.setattr(database, "Connector", lambda **kwargs: calls.append("connector"))
    with pytest.raises(rt.SecureUnavailable):
        database._build_engine()
    assert calls == []


def test_stale_generation_reconciliation_closes_without_drain_claim(service_case):
    rt = runtime_api()
    case = service_case
    rt.reconcile_stale_generation(case["engine"], rt.Invocation("synthetic.service", uuid4(), uuid4(), 1000))
    with case["engine"].connect() as db:
        assert db.scalar(text("SELECT admission FROM security_control")) == "CLOSED"
        assert db.scalar(text("SELECT state FROM credential_generations")) == "FAILED"


def test_admission_wait_timeout_does_not_open_database_or_call_handler():
    rt = runtime_api()
    clock = [0]
    lock = FakeLock()
    lock.held = True
    def monotonic():
        clock[0] += 1
        return clock[0]
    calls, sent = [], []
    runtime = rt.SecureRuntime(lock, check_admission=lambda: calls.append("sql"), monotonic=monotonic)
    async def app(scope, receive, send):
        calls.append("handler")
    async def send(message):
        sent.append(message)
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
    assert calls == [] and lock.releases == 0
    assert sent[0]["status"] == 503 and (b"retry-after", b"2") in sent[0]["headers"]


@pytest.mark.parametrize("size,status", [(20 * 1024 * 1024, 200), (20 * 1024 * 1024 + 1, 413)])
def test_authenticated_media_body_exact_limit(size, status):
    rt = runtime_api()
    runtime = rt.SecureRuntime(FakeLock(), check_admission=lambda: None)
    starts, total, chunks = [], [], []
    async def app(scope, receive, send):
        scope["secure_media"] = True
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"video/mp4")]})
        await send({"type": "http.response.body", "body": b"a" * size})
    async def send(message):
        if message["type"] == "http.response.start":
            starts.append(message["status"])
        else:
            total.append(len(message["body"]))
            chunks.append(message["more_body"])
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
    assert starts == [status] and max(total) <= 65536 and chunks[-1] is False
    assert sum(total) == size if status == 200 else sum(total) < 100


@pytest.mark.parametrize("case,status", [("range", 416), ("unfinished", 503), ("file_response", 503)])
def test_unbounded_response_forms_fail_closed(case, status):
    rt = runtime_api()
    runtime = rt.SecureRuntime(FakeLock(), check_admission=lambda: None)
    sent = []
    async def app(scope, receive, send):
        assert case != "range"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if case == "file_response":
            await send({"type": "http.response.pathsend", "path": "synthetic-only"})
        else:
            await send({"type": "http.response.body", "body": b"protected", "more_body": True})
    async def send(message):
        sent.append(message)
    async def receive():
        await asyncio.Future()
    scope = {"type": "http", "headers": [(b"range", b"bytes=0-1")] if case == "range" else []}
    asyncio.run(rt.SecureBoundary(app, runtime)(scope, receive, send))
    assert sent[0]["status"] == status
    assert b"protected" not in b"".join(m.get("body", b"") for m in sent)


def test_committed_database_write_survives_lost_response_without_sql_during_send(service_case):
    rt = runtime_api()
    from test_transaction_boundary import SessionOpenTracker
    case = service_case
    tracker = SessionOpenTracker(case["engine"])
    lock = FakeLock()
    runtime = rt.SecureRuntime(lock, check_admission=lambda: None)
    starts = []
    async def app(scope, receive, send):
        with tracker() as db:
            db.execute(text("UPDATE security_scopes SET revision=revision+1 WHERE domain='room'"))
            db.commit()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    async def send(message):
        assert tracker.open_count == 0 and lock.held
        if message["type"] == "http.response.start":
            starts.append(message["status"])
        else:
            raise RuntimeError("synthetic transport failure with secret")
    async def receive():
        await asyncio.Future()
    asyncio.run(rt.SecureBoundary(app, runtime)({"type": "http", "headers": []}, receive, send))
    with case["engine"].connect() as db:
        assert db.scalar(text("SELECT revision FROM security_scopes WHERE domain='room'")) == 2
    assert starts == [200] and lock.releases == 1


def test_startup_exposes_validated_vault_and_store_without_sql_during_hooks(service_case, tmp_path, monkeypatch):
    rt = runtime_api()
    from app.core.config import Settings
    from app.storage import key_vault, local
    from app.storage.contracts import KeyRef
    from sqlalchemy import event
    case = service_case
    inv, boot = uuid4(), uuid4()
    ref = KeyRef("room", case["room"], case["key"], 1)
    with case["engine"].begin() as db:
        control_update(db, admission="CLOSED")
        db.execute(text("UPDATE credential_generations SET state='DRAINING',revision=3"))
        db.execute(text("UPDATE credential_generations SET state='DRAINED',revision=4"))
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=str(ref.key_id), key_epoch=1,
            domain="room", owner_id=str(ref.owner_id))
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    name_for_ref = key_vault.SystemdKeyVault.credential_name
    (credentials / name_for_ref(ref)).touch()
    configured = Settings(_env_file=None, security_mode="SECURE", database_url=str(case["engine"].url),
        use_gcs_media_storage=False, use_cloud_sql_connector=False, prefer_ollama_for_gemma=False,
        jwt_secret_key="synthetic-test-only-signing-key-00000000000", secure_generation_id=gid,
        secure_unit_name="synthetic.service", secure_approved_uid=1000,
        secure_credentials_directory=str(credentials), secure_ciphertext_directory=str(tmp_path / "ciphertext"),
        secure_lock_path=str(tmp_path / "operation.lock"), secure_worker_lock_path=str(tmp_path / "worker.lock"))
    checked_out = []
    event.listen(case["engine"], "checkout", lambda *args: checked_out.append(1))
    event.listen(case["engine"], "checkin", lambda *args: checked_out.pop())
    class Lock(FakeLock):
        def __init__(self, *args, **kwargs):
            super().__init__()
        @property
        def fd(self):
            return 10 if self.held else None
    class Vault:
        credential_name = staticmethod(name_for_ref)
        def __init__(self, **kwargs):
            assert not checked_out
        def load(self, supplied):
            assert supplied == ref and not checked_out
            return bytes(32)
    class Store:
        def __init__(self, root):
            assert not checked_out
    monkeypatch.setattr(rt.sys, "platform", "linux")
    monkeypatch.setattr(rt.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(rt, "LinuxLock", Lock)
    monkeypatch.setattr(key_vault, "SystemdKeyVault", Vault)
    monkeypatch.setattr(local, "LocalCiphertextStore", Store)
    monkeypatch.setenv("INVOCATION_ID", str(inv))
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: str(boot) if str(path).endswith("boot_id") else read_text(path, *args, **kwargs))
    runtime = rt.startup_runtime(case["engine"], configured)
    try:
        assert isinstance(runtime.vault, Vault) and isinstance(runtime.store, Store)
        rt.check_generation(case["engine"], UUID(gid), rt.Invocation("synthetic.service", inv, boot, 1000))
    finally:
        runtime.worker_lock.release()



def test_secure_mode_rejects_database_advanced_to_0006(tmp_path):
    rt = runtime_api()
    url = f"sqlite:///{tmp_path / 'secure-0006.sqlite'}"
    command.upgrade(_build_alembic_config(url), "0006_family_graph")
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            control_update(db, mode="CUTOVER")
            control_update(db, mode="SECURE")
        with pytest.raises(rt.SecureUnavailable):
            rt.validate_catalogue_mode(engine, "SECURE")
    finally:
        engine.dispose()


@pytest.mark.parametrize("table_name", ["family_people", "family_relationships"])
def test_secure_mode_rejects_plaintext_graph_table_with_0005_stamp(tmp_path, table_name):
    rt = runtime_api()
    url = f"sqlite:///{tmp_path / ('secure-extra-' + table_name + '.sqlite')}"
    command.upgrade(_build_alembic_config(url), "0006_family_graph")
    engine = fk_enforced_engine(url)
    try:
        other_table = "family_relationships" if table_name == "family_people" else "family_people"
        with engine.connect() as db:
            db.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.commit()
            db.exec_driver_sql(f"DROP TABLE {other_table}")
            db.exec_driver_sql("UPDATE alembic_version SET version_num='0005_secure_catalogue'")
            db.commit()
        with engine.begin() as db:
            control_update(db, mode="CUTOVER")
            control_update(db, mode="SECURE")
        with pytest.raises(rt.SecureUnavailable):
            rt.validate_catalogue_mode(engine, "SECURE")
    finally:
        engine.dispose()


def test_secure_mode_accepts_clean_0005_catalogue(tmp_path):
    rt = runtime_api()
    url = f"sqlite:///{tmp_path / 'secure-clean-0005.sqlite'}"
    command.upgrade(_build_alembic_config(url), "0005_secure_catalogue")
    engine = fk_enforced_engine(url)
    try:
        with engine.begin() as db:
            control_update(db, mode="CUTOVER")
            control_update(db, mode="SECURE")
        rt.validate_catalogue_mode(engine, "SECURE")
    finally:
        engine.dispose()
