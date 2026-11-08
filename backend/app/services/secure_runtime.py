"""One invocation, one guarded operation through the final bounded ASGI send.

Provisioning, cgroup supervision and credential removal are external operator work.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import json
import os
from pathlib import Path
import stat
import sys
import time
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import inspect, text

from app.storage.contracts import KeyRef, secret_bytes


class SecureUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("credential_unavailable")


class RuntimeFailStop(BaseException):
    """A supervisor/test fail-stop callback must not resume HTTP error handling."""


class LinuxLock:
    """Preprovisioned private inode; never create, unlink or inherit a lock domain."""
    def __init__(self, path, *, approved_uid):
        if sys.platform != "linux" or os.geteuid() != approved_uid:
            raise SecureUnavailable()
        self.path, self.uid, self.pid = Path(path), approved_uid, os.getpid()
        self.fd, self.inode = None, None

    def try_acquire(self):
        import fcntl
        if os.getpid() != self.pid or os.geteuid() != self.uid:
            raise SecureUnavailable()
        if self.fd is not None:
            return False
        directory = fd = None
        try:
            if not self.path.is_absolute() or ".." in self.path.parts:
                raise OSError()
            directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            for part in self.path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                os.close(directory)
                directory = child
            parent = os.fstat(directory)
            if parent.st_uid != self.uid or stat.S_IMODE(parent.st_mode) != 0o700:
                raise OSError()
            fd = os.open(self.path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(fd)
            inode = (info.st_dev, info.st_ino)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or (self.inode is not None and inode != self.inode)):
                raise OSError()
            self.inode = inode
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            if os.stat(self.path.name, dir_fd=directory, follow_symlinks=False).st_ino != info.st_ino:
                raise OSError()
            self.fd, fd = fd, None
            return True
        except OSError:
            raise SecureUnavailable() from None
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)

    def release(self):
        if os.getpid() != self.pid or self.fd is None:
            raise SecureUnavailable()
        fd, self.fd = self.fd, None
        os.close(fd)


@dataclass(frozen=True)
class Invocation:
    unit_name: str
    invocation_id: UUID
    boot_id: UUID
    approved_uid: int

    def __post_init__(self):
        if (type(self.unit_name) is not str or not 1 <= len(self.unit_name) <= 255
                or type(self.invocation_id) is not UUID or type(self.boot_id) is not UUID
                or type(self.approved_uid) is not int or self.approved_uid <= 0):
            raise SecureUnavailable()


def validate_catalogue_mode(engine, configured_mode):
    """Read-only startup check; an unknown secure schema never selects legacy."""
    try:
        with engine.connect() as db:
            tables = set(inspect(db).get_table_names())
            if "security_control" not in tables:
                if configured_mode == "LEGACY" and not any(t in tables for t in ("key_epochs", "protected_pointers", "credential_generations")):
                    return
                raise SecureUnavailable()
            row = db.execute(text("SELECT * FROM security_control WHERE id=1")).mappings().one()
            if configured_mode == "LEGACY":
                if row["mode"] != "LEGACY" or row["admission"] != "CLOSED":
                    raise SecureUnavailable()
            elif configured_mode == "SECURE":
                from scripts.adopt_alembic import verify_revision
                revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                if (db.dialect.name != "sqlite" or row["mode"] != "SECURE" or row["schema_version"] != 1
                        or db.scalar(text("PRAGMA foreign_keys")) != 1
                        or revision not in ("0005_secure_catalogue", "0009_ocr_review", "0012_secure_room_storage_quota", "0013_secure_reconciliation", "0014_native_text", "0015_record_privacy", "0016_family_context")
                        or verify_revision(db, revision)):
                    raise SecureUnavailable()
                # 0009 is the documented feature schema, but its LEGACY graph
                # has no encrypted representation and must remain empty.
                if revision in ("0009_ocr_review", "0012_secure_room_storage_quota", "0013_secure_reconciliation", "0014_native_text", "0015_record_privacy", "0016_family_context") and any(
                        db.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None
                        for table in (("family_people", "family_relationships") if revision == "0009_ocr_review" else
                                      ("family_people", "family_relationships", "albums", "album_photos",
                                       "photo_upload_batches", "photo_upload_items", "album_videos", "record_read_receipts"))):
                    raise SecureUnavailable()
                if revision == "0016_family_context" and any(
                        db.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None
                        for table in ("context_entities", "context_sources", "context_relations",
                                      "relation_aliases", "relation_alias_targets", "context_events",
                                      "event_participation", "external_dataset_refs")):
                    raise SecureUnavailable()
            else:
                raise SecureUnavailable()
    except Exception:
        raise SecureUnavailable() from None


def _generation_rows(db, generation_id):
    c = db.execute(text("SELECT * FROM security_control WHERE id=1")).mappings().one()
    g = db.execute(text("SELECT * FROM credential_generations WHERE generation_id=:id"), dict(id=str(generation_id))).mappings().one()
    rows = db.execute(text("SELECT k.*,s.state AS scope_state,s.revision AS scope_revision FROM credential_generation_keys m JOIN key_epochs k ON k.key_id=m.key_id AND k.epoch=m.key_epoch AND k.domain=m.domain AND k.owner_id=m.owner_id JOIN security_scopes s ON s.domain=k.domain AND s.owner_id=k.owner_id WHERE m.generation_id=:id ORDER BY k.domain,k.owner_id,k.key_id,k.epoch"), dict(id=str(generation_id))).mappings().all()
    if (c["mode"] != "SECURE" or c["schema_version"] != 1 or c["catalogue_id"] != g["catalogue_id"]
            or any(k["state"] not in ("ACTIVE", "DECRYPT_ONLY", "PREPARED") or k["provisioning_state"] != "PERSISTED" or k["scope_state"] != "ACTIVE" for k in rows)):
        raise SecureUnavailable()
    return dict(c), dict(g), tuple(dict(k) for k in rows)


def check_generation(engine, generation_id, invocation):
    try:
        with engine.connect() as db:
            c, g, _ = _generation_rows(db, generation_id)
            if (c["admission"] != "OPEN" or c["runtime_generation"] != str(generation_id) or g["state"] != "VERIFIED"
                    or (g["unit_name"], g["invocation_id"], g["boot_id"], g["approved_uid"]) != (invocation.unit_name, str(invocation.invocation_id), str(invocation.boot_id), invocation.approved_uid)):
                raise SecureUnavailable()
    except Exception:
        raise SecureUnavailable() from None


def verify_generation(engine, *, generation_id, invocation, vault, delivered_names):
    """CLOSED read / exact external load / fresh CAS, no secret survives this call."""
    from app.storage.key_vault import SystemdKeyVault
    try:
        with engine.connect() as db:
            before = _generation_rows(db, generation_id)
        c, g, keys = before
        if (c["admission"] != "CLOSED" or g["state"] != "REQUESTED" or g["control_revision"] != c["revision"]
                or (g["unit_name"], g["approved_uid"]) != (invocation.unit_name, invocation.approved_uid)):
            raise SecureUnavailable()
        refs = tuple(KeyRef(k["domain"], UUID(k["owner_id"]), UUID(k["key_id"]), k["epoch"]) for k in keys)
        expected = {SystemdKeyVault.credential_name(ref) for ref in refs}
        if set(delivered_names()) != expected:
            raise SecureUnavailable()
        for ref in refs:
            secret_bytes(vault.load(ref), 32)
        if set(delivered_names()) != expected:
            raise SecureUnavailable()
        with engine.begin() as db:
            if _generation_rows(db, generation_id) != before:
                raise SecureUnavailable()
            from app.services.secure_catalogue import _cas
            _cas(db, "UPDATE credential_generations SET state='VERIFIED',revision=revision+1,invocation_id=:inv,boot_id=:boot WHERE generation_id=:id AND revision=:r AND state='REQUESTED'",
                 inv=str(invocation.invocation_id), boot=str(invocation.boot_id), id=str(generation_id), r=g["revision"])
            _cas(db, "UPDATE security_control SET runtime_generation=:g,revision=revision+1 WHERE id=1 AND revision=:r AND admission='CLOSED'", g=str(generation_id), r=c["revision"])
            _cas(db, "UPDATE security_control SET admission='OPEN',revision=revision+1 WHERE id=1 AND revision=:r AND admission='CLOSED'", r=c["revision"] + 1)
    except Exception:
        # A failed delivery/proof consumes its immutable generation ID. If the
        # catalogue itself is unreadable the external operator must reconcile it.
        try:
            with engine.begin() as db:
                failed = db.execute(text("SELECT revision,state FROM credential_generations WHERE generation_id=:id"), dict(id=str(generation_id))).mappings().one_or_none()
                if failed and failed["state"] == "REQUESTED":
                    from app.services.secure_catalogue import _cas
                    _cas(db, "UPDATE credential_generations SET state='FAILED',revision=revision+1 WHERE generation_id=:id AND state='REQUESTED' AND revision=:r", id=str(generation_id), r=failed["revision"])
        except Exception:
            pass
        raise SecureUnavailable() from None


def reconcile_stale_generation(engine, invocation):
    """Unexpected invocation replacement closes durably; never infers DRAINED."""
    try:
        with engine.begin() as db:
            c = db.execute(text("SELECT * FROM security_control WHERE id=1")).mappings().one()
            if c["runtime_generation"] is None:
                return
            g = db.execute(text("SELECT * FROM credential_generations WHERE generation_id=:id"), dict(id=c["runtime_generation"])).mappings().one()
            if g["state"] == "VERIFIED":
                # Every startup needs a newly REQUESTED generation, even same invocation.
                from app.services.secure_catalogue import _cas
                _cas(db, "UPDATE security_control SET admission='CLOSED',revision=revision+1 WHERE id=1 AND revision=:r", r=c["revision"])
                _cas(db, "UPDATE credential_generations SET state='FAILED',revision=revision+1 WHERE generation_id=:id AND state='VERIFIED' AND revision=:r", id=g["generation_id"], r=g["revision"])
    except Exception:
        raise SecureUnavailable() from None


@dataclass
class GuardToken:
    runtime: object
    task: object
    pid: int
    active: bool = True
    poisoned: bool = False


_current_guard = ContextVar("secure_operation_guard", default=None)
_ADMISSION_QUEUE_LIMIT = 16
_ADMISSION_QUEUE_TIMEOUT = 90


class SecureRuntime:
    def __init__(self, lock, *, check_admission, monotonic=time.monotonic, fail_stop=None):
        self.lock, self.check_admission = lock, check_admission
        self.monotonic, self.fail_stop = monotonic, fail_stop or (lambda: os._exit(70))
        self.pid = os.getpid()
        self.worker_lock = None
        self._admission_lock = asyncio.Lock()
        self._admission_pending = 0

    @asynccontextmanager
    async def _admission_slot(self):
        if self._admission_pending >= _ADMISSION_QUEUE_LIMIT:
            raise SecureUnavailable()
        self._admission_pending += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._admission_lock.acquire(), _ADMISSION_QUEUE_TIMEOUT)
            except TimeoutError:
                raise SecureUnavailable() from None
            acquired = True
            yield
        finally:
            if acquired:
                self._admission_lock.release()
            self._admission_pending -= 1

    @contextmanager
    def closed_destruction_guard(self):
        """Use the established native lock for a short, already-admission-closed transition."""
        if os.getpid() != self.pid or _current_guard.get() is not None:
            raise SecureUnavailable()
        if not self.lock.try_acquire():
            raise SecureUnavailable()
        try:
            yield
        finally:
            self.lock.release()

    @asynccontextmanager
    async def operation_guard(self, token=None):
        if os.getpid() != self.pid:
            raise SecureUnavailable()
        current = _current_guard.get()
        if token is not None:
            if (token is not current or token.runtime is not self or not token.active or token.poisoned
                    or token.pid != os.getpid() or token.task is not asyncio.current_task()):
                raise SecureUnavailable()
            yield token
            return
        if current is not None:
            raise SecureUnavailable()  # Nested services must explicitly reuse their token.
        async with self._admission_slot():
            deadline = self.monotonic() + 2
            while not self.lock.try_acquire():
                if self.monotonic() >= deadline:
                    raise SecureUnavailable()
                await asyncio.sleep(min(0.02, max(0, deadline - self.monotonic())))
            token = GuardToken(self, asyncio.current_task(), os.getpid())
            context = _current_guard.set(token)
            try:
                self.check_admission()
                yield token
            finally:
                token.active = False
                _current_guard.reset(context)
                if not token.poisoned:
                    self.lock.release()


def operation_guard(token=None):
    """Shared nested service entry; top-level owners use their SecureRuntime."""
    current = token or _current_guard.get()
    if current is None:
        raise SecureUnavailable()
    return current.runtime.operation_guard(current)


class SecureBoundary:
    """Buffer authenticated, finite responses before start; retain guard through send.

    App handlers must finish authentication/revalidation and close SQL before returning.
    `scope['secure_media'] = True` is a trusted handler flag for a single authenticated
    media object; clients cannot select the larger media limit via headers.
    """
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        timings = scope.get("secure_timings")
        started_ns = time.perf_counter_ns() if isinstance(timings, dict) else None
        try:
            async with self.runtime.operation_guard() as token:
                if started_ns is not None:
                    timings["queue_wait_ns"] = time.perf_counter_ns() - started_ns
                scope["secure_guard"] = token
                start, body, terminal = None, bytearray(), False
                async def capture(message):
                    nonlocal start, terminal
                    if terminal:
                        raise SecureUnavailable()
                    if message["type"] == "http.response.start" and start is None:
                        headers = message.get("headers", [])
                        if (sum(len(k) + len(v) for k, v in headers) > 65536
                                or any(k.lower() in (b"content-encoding", b"content-range") for k, v in headers)):
                            raise OverflowError()
                        content_types = [v for k, v in headers if k.lower() == b"content-type"]
                        content_type = content_types[0] if content_types else b"application/json"
                        allowed = (b"application/json", b"application/octet-stream", b"image/jpeg", b"image/png",
                                   b"image/webp", b"image/gif", b"video/mp4", b"video/webm", b"audio/mpeg", b"audio/wav",
                                   b"text/plain; charset=utf-8")
                        if len(content_types) > 1 or content_type not in allowed:
                            raise SecureUnavailable()
                        # Rebuild fixed headers so names, filenames, tokens and caches cannot leak content.
                        safe_headers = [(b"content-type", content_type), (b"cache-control", b"no-store"),
                                        (b"x-content-type-options", b"nosniff")]
                        record_version = scope.get("record_response_version")
                        if type(record_version) is int and record_version > 0:
                            safe_headers.append((b"etag", f'"{record_version}"'.encode("ascii")))
                        if content_type == b"text/plain; charset=utf-8":
                            safe_headers.extend([(b"content-disposition", b"attachment"),
                                (b"content-security-policy", b"sandbox; default-src 'none'")])
                        if message["status"] in (202, 503):
                            safe_headers.append((b"retry-after", b"2"))
                        activation_id = scope.get("secure_activation_location")
                        if message["status"] == 202 and type(activation_id) is UUID:
                            safe_headers.append((b"location", ("/api/v1/secure-operations/" + str(activation_id)).encode("ascii")))
                        start = {"type": "http.response.start", "status": message["status"], "headers": safe_headers}
                    elif message["type"] == "http.response.body" and start is not None:
                        chunk = message.get("body", b"")
                        limit = 20 * 1024 * 1024 if scope.get("secure_media") is True else 4 * 1024 * 1024
                        if type(chunk) is not bytes or len(body) + len(chunk) > limit:
                            raise OverflowError()
                        body.extend(chunk)
                        terminal = not message.get("more_body", False)
                    else:
                        raise SecureUnavailable()
                try:
                    if any(k.lower() == b"range" for k, v in scope.get("headers", [])):
                        start, body, terminal = self._error(416)
                    else:
                        await self.app(scope, receive, capture)
                    if not terminal or start is None:
                        raise SecureUnavailable()
                    self.runtime.check_admission()
                except OverflowError:
                    start, body, terminal = self._error(413)
                except Exception:
                    start, body, terminal = self._error(503)
                try:
                    await self._send_response(start, body, receive, send, token)
                finally:
                    body.clear()
                    scope.pop("secure_guard", None)
        except SecureUnavailable:
            # No protected allocation/SQL occurred while waiting for admission.
            start, body, _ = self._error(503)
            await self._send_response(start, body, receive, send, None)
        finally:
            if started_ns is not None:
                timings["api_ns"] = time.perf_counter_ns() - started_ns

    @staticmethod
    def _error(status):
        headers = [(b"content-type", b"application/json")]
        if status == 503:
            headers.append((b"retry-after", b"2"))
        return ({"type": "http.response.start", "status": status, "headers": headers},
                bytearray(b'{"detail":"secure_request_unavailable"}'), True)

    async def _send_response(self, start, body, receive, send, token):
        started = None
        async def bounded_send(message):
            if self.runtime.monotonic() - started >= 15:
                raise TimeoutError()
            await send(message)
        async def sender():
            await bounded_send(start)
            if not body:
                await bounded_send({"type": "http.response.body", "body": b"", "more_body": False})
            for offset in range(0, len(body), 65536):
                await bounded_send({"type": "http.response.body", "body": bytes(body[offset:offset + 65536]),
                            "more_body": offset + 65536 < len(body)})
        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                await asyncio.sleep(0)
        sending, disconnect = None, asyncio.create_task(disconnected())
        try:
            # Observe a queued disconnect before handing even headers to transport.
            await asyncio.sleep(0)
            if disconnect.done():
                return
            started = self.runtime.monotonic()
            sending = asyncio.create_task(sender())
            done, _ = await asyncio.wait((sending, disconnect), timeout=max(0, 15 - (self.runtime.monotonic() - started)), return_when=asyncio.FIRST_COMPLETED)
            if sending in done:
                # Read exceptions without disclosing transport payloads or sending twice.
                try:
                    sending.result()
                except Exception:
                    pass
        finally:
            tasks = tuple(t for t in (sending, disconnect) if t is not None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Repeated request cancellation cannot cancel the termination proof.
            cleanup = asyncio.create_task(asyncio.wait(tasks, timeout=1))
            cancelled = False
            while True:
                try:
                    _, alive = await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if alive:
                if token is not None:
                    token.poisoned = True
                self.runtime.fail_stop()
                raise RuntimeFailStop()
            for task in tasks:
                if not task.cancelled():
                    task.exception()
            if cancelled:
                raise asyncio.CancelledError()


# Only the durable GCS binding validator can register namespace provenance.
_catalogue_gcs_stores = WeakKeyDictionary()


def verified_gcs_binding(db, store):
    saved = _catalogue_gcs_stores.get(store)
    if saved is None:
        return None
    catalogue_id = db.scalar(text("SELECT catalogue_id FROM security_control WHERE id=1"))
    return saved[1] if saved == (catalogue_id, store.storage_binding) else None


def ciphertext_store(engine, settings, approved_uid):
    """One backend per catalogue; a private durable sidecar prevents bucket retargeting.

    The operator provisions <database>.gcs-binding.json alongside the catalogue.
    No runtime TOFU, manifest rewrite, object migration, or transport fallback.
    Same-uid/root changes to catalogue and binding are outside this boundary.
    """
    from app.storage.key_vault import _directory, _read_entry
    try:
        adapter = settings.secure_ciphertext_adapter
        with engine.connect() as db:
            catalogue_id = db.scalar(text("SELECT catalogue_id FROM security_control WHERE id=1"))
            adapters = set(db.scalars(text("SELECT DISTINCT adapter FROM encrypted_objects")))
        if not catalogue_id or adapters - {adapter}:
            raise SecureUnavailable()
        path = (Path(engine.url.database).absolute()
                if engine.url.database not in (None, "", ":memory:") else None)
        if adapter == "local":
            if path is not None:
                try:
                    # Even malformed/dangling bindings retain GCS authority; never ignore them.
                    os.lstat(path.with_name(path.name + ".gcs-binding.json"))
                except FileNotFoundError:
                    pass
                else:
                    raise SecureUnavailable()
            from app.storage.local import LocalCiphertextStore
            return LocalCiphertextStore(settings.secure_ciphertext_directory)
        if (adapter != "gcs" or path is None
                or any(name in os.environ for name in ("STORAGE_EMULATOR_HOST", "API_ENDPOINT_OVERRIDE"))):
            raise SecureUnavailable()
        with _directory(path.parent, approved_uid) as directory:
            raw = _read_entry(directory, path.name + ".gcs-binding.json", approved_uid, 4096)
        bound = json.loads(raw)
        if (type(bound) is not dict or type(bound.get("version")) is not int
                or bound != dict(version=1, catalogue_id=catalogue_id,
                                 bucket=settings.secure_gcs_bucket, project_id=settings.secure_gcs_project_id)):
            raise SecureUnavailable()
        # ADC is resolved only after local binding and catalogue checks; keys stay in the vault.
        from google.cloud import storage
        from app.storage.gcs import GCSCiphertextStore
        endpoint = "https://storage.googleapis.com"
        client = storage.Client(project=bound["project_id"], client_options={"api_endpoint": endpoint})
        if client.api_endpoint != endpoint:
            raise SecureUnavailable()
        store = GCSCiphertextStore(client.bucket(bound["bucket"]))
        _catalogue_gcs_stores[store] = (catalogue_id, store.storage_binding)
        return store
    except Exception:
        raise SecureUnavailable() from None


def startup_runtime(engine, settings):
    """Real Linux invocation entry; all custody is preprovisioned externally."""
    from app.core.config import validate_secure_config
    from app.storage.key_vault import SystemdKeyVault
    validate_secure_config(settings)
    validate_catalogue_mode(engine, "SECURE")
    worker = lock = None
    try:
        if sys.platform != "linux" or os.geteuid() != settings.secure_approved_uid or os.geteuid() == 0:
            raise SecureUnavailable()
        invocation = Invocation(settings.secure_unit_name, UUID(os.environ["INVOCATION_ID"]),
                                UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()), settings.secure_approved_uid)
        worker = LinuxLock(settings.secure_worker_lock_path, approved_uid=invocation.approved_uid)
        if not worker.try_acquire():
            raise SecureUnavailable()
        lock = LinuxLock(settings.secure_lock_path, approved_uid=invocation.approved_uid)
        if not lock.try_acquire():
            raise SecureUnavailable()
        reconcile_stale_generation(engine, invocation)
        vault = SystemdKeyVault(credentials_directory=settings.secure_credentials_directory, approved_uid=invocation.approved_uid)
        store = ciphertext_store(engine, settings, invocation.approved_uid)
        gid = UUID(settings.secure_generation_id)
        verify_generation(engine, generation_id=gid, invocation=invocation, vault=vault,
                          delivered_names=lambda: {p.name for p in Path(settings.secure_credentials_directory).iterdir()})
        runtime = SecureRuntime(lock, check_admission=lambda: check_generation(engine, gid, invocation))
        runtime.worker_lock = worker
        runtime.vault, runtime.store = vault, store
        from app.services.secure_local_providers import configure
        configure(runtime, settings)
        logging.getLogger("uvicorn.access").disabled = True
        logging.getLogger("sqlalchemy.engine").disabled = True
        logging.getLogger("sqlalchemy.engine.Engine").disabled = True
        return runtime
    except Exception:
        if worker is not None and worker.fd is not None:
            worker.release()
        raise SecureUnavailable() from None
    finally:
        if lock is not None and lock.fd is not None:
            lock.release()
