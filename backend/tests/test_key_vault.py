"""Fake systemd transport, real Linux descriptors; never provision a host key."""
import dataclasses
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
from uuid import UUID

import pytest


def test_vault_failclosed_unsupported_platform(monkeypatch, tmp_path):
    from app.storage import key_vault
    from app.storage.contracts import VaultUnavailable
    monkeypatch.setattr(key_vault.sys, "platform", "win32")
    with pytest.raises(VaultUnavailable):
        key_vault.SystemdKeyVault(credentials_directory=tmp_path, approved_uid=1000)


linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor/mode behavior requires DGX Linux")


@linux
def test_runtime_missing_mapping_permissions_and_revocation(tmp_path):
    from app.storage.key_vault import SystemdKeyVault
    from app.storage.contracts import KeyRef, VaultUnavailable
    if os.geteuid() == 0:
        pytest.skip("Requires actual non-root runtime identity")
    os.chmod(tmp_path, 0o700)
    ref = KeyRef("room", UUID(int=1), UUID(int=2), 1)
    vault = SystemdKeyVault(credentials_directory=tmp_path, approved_uid=os.geteuid())
    with pytest.raises(VaultUnavailable):
        vault.load(ref)
    entry = tmp_path / vault.credential_name(ref)
    value = ["ambient-legacy/key", 1, "room", str(ref.owner_id), str(ref.key_id), 1, "ab" * 32]
    entry.write_text(json.dumps(value, separators=(",", ":")))
    entry.chmod(0o400)
    assert vault.load(ref) == bytes.fromhex("ab" * 32)
    with pytest.raises(VaultUnavailable):
        vault.load(dataclasses.replace(ref, domain="user"))
    vault.revoke_cache(ref)
    with pytest.raises(VaultUnavailable):
        vault.load(ref)
    with pytest.raises(VaultUnavailable):
        vault.prepare(ref)
    # No secret cache: changed delivered entry must be read and rejected.
    other = SystemdKeyVault(credentials_directory=tmp_path, approved_uid=os.geteuid())
    entry.chmod(0o600)
    entry.write_text("secret-synthetic-invalid")
    entry.chmod(0o400)
    with pytest.raises(VaultUnavailable) as error:
        other.load(ref)
    assert "secret-synthetic" not in str(error.value)
    entry.chmod(0o644)
    with pytest.raises(VaultUnavailable):
        other.load(ref)


@linux
def test_operator_prepare_no_replace_destroy_requires_exact_drained_approval(tmp_path, monkeypatch):
    from app.storage import key_vault
    from app.storage.contracts import KeyRef, ExactTargetApproval, VaultUnavailable, StorageConflict
    os.chmod(tmp_path, 0o700)
    uid = os.geteuid()
    monkeypatch.setattr(key_vault.os, "geteuid", lambda: 0)
    real_directory, real_read = key_vault._directory, key_vault._read_entry
    # Translate synthetic root ownership only; descriptor, no-follow, modes and reads stay real.
    monkeypatch.setattr(key_vault, "_directory", lambda path, owner: real_directory(path, uid))
    monkeypatch.setattr(key_vault, "_read_entry", lambda directory, name, owner, limit: real_read(directory, name, uid, limit))
    # Fake only OS identity and external encryption; filesystem safety stays real.
    calls = []
    def encrypt(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, b"synthetic-host-encrypted:" + kwargs["input"], b"")
    monkeypatch.setattr(key_vault.subprocess, "run", encrypt)
    ref = KeyRef("room", UUID(int=1), UUID(int=2), 1)
    revision = UUID(int=8)
    vault = key_vault.SystemdKeyVault(provisioning_directory=tmp_path, approved_uid=uid,
                                    operator=True, inventory_revision=revision)
    vault.prepare(ref)
    assert calls[0][0][:3] == ["/usr/bin/systemd-creds", "encrypt", "--with-key=host"]
    assert calls[0][0][-2:] == ["-", "-"]
    assert len(json.loads(calls[0][1])[-1]) == 64
    with pytest.raises(StorageConflict):
        vault.prepare(ref)
    other = dataclasses.replace(ref, domain="user", key_id=UUID(int=3))
    vault.prepare(other)
    assert json.loads(calls[-1][1])[-1] != json.loads(calls[0][1])[-1]
    approval = ExactTargetApproval(UUID(int=9), ref.domain, ref.owner_id, ref.key_id, 1,
        datetime.now(timezone.utc) + timedelta(minutes=1), revision, True)
    for invalid in (dataclasses.replace(approval, references_drained=False),
                    dataclasses.replace(approval, epoch=2),
                    dataclasses.replace(approval, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))):
        with pytest.raises(VaultUnavailable):
            vault.destroy(ref, invalid)
    vault.destroy(ref, approval)
    assert not (tmp_path / vault.credential_name(ref)).exists()
    assert (tmp_path / vault.credential_name(other)).exists()


@linux
def test_runtime_rejects_wrong_uid_root_symlink_and_oversized_entry(tmp_path):
    from app.storage.key_vault import SystemdKeyVault
    from app.storage.contracts import KeyRef, VaultUnavailable
    os.chmod(tmp_path, 0o700)
    with pytest.raises(VaultUnavailable):
        SystemdKeyVault(credentials_directory=tmp_path, approved_uid=os.geteuid() + 1)
    if os.geteuid() == 0:
        return
    vault = SystemdKeyVault(credentials_directory=tmp_path, approved_uid=os.geteuid())
    ref = KeyRef("lookup", UUID(int=1), UUID(int=2), 1)
    entry = tmp_path / vault.credential_name(ref)
    target = tmp_path / "target"
    target.write_bytes(b"x" * 4097)
    target.chmod(0o400)
    entry.symlink_to(target)
    with pytest.raises(VaultUnavailable):
        vault.load(ref)
    entry.unlink()
    entry.write_bytes(b"x" * 4097)
    entry.chmod(0o400)
    with pytest.raises(VaultUnavailable):
        vault.load(ref)


def _synthetic_operator(directory, monkeypatch):
    """Fake privilege/encryption only, preserving real non-root Linux file operations."""
    from app.storage import key_vault
    uid = os.geteuid()
    real_directory, real_read = key_vault._directory, key_vault._read_entry
    monkeypatch.setattr(key_vault.os, "geteuid", lambda: 0)
    monkeypatch.setattr(key_vault, "_directory", lambda path, owner: real_directory(path, uid))
    monkeypatch.setattr(key_vault, "_read_entry", lambda descriptor, name, owner, limit: real_read(descriptor, name, uid, limit))
    monkeypatch.setattr(key_vault.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 0, b"synthetic-host-encrypted:" + kwargs["input"], b""))
    return key_vault.SystemdKeyVault(provisioning_directory=directory, approved_uid=uid,
                                    operator=True, inventory_revision=UUID(int=8))


@linux
@pytest.mark.parametrize("expires_during", ["thread_lock", "directory_lock", "read"])
def test_destroy_rechecks_approval_after_waits_and_before_unlink(tmp_path, monkeypatch, expires_during):
    from app.storage import contracts, key_vault
    import fcntl
    tmp_path.chmod(0o700)
    vault = _synthetic_operator(tmp_path, monkeypatch)
    ref = contracts.KeyRef("room", UUID(int=1), UUID(int=2), 1)
    entry = tmp_path / vault.credential_name(ref)
    entry.write_bytes(b"synthetic persisted ciphertext")
    entry.chmod(0o600)
    clock = [datetime.now(timezone.utc)]
    approval = contracts.ExactTargetApproval(UUID(int=9), ref.domain, ref.owner_id, ref.key_id, 1,
        clock[0] + timedelta(seconds=1), UUID(int=8), True)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(contracts, "datetime", Clock)
    def expire():
        clock[0] += timedelta(seconds=2)
    if expires_during == "thread_lock":
        real_lock = vault._lock
        class DelayedLock:
            def __enter__(self):
                real_lock.acquire()
                expire()
            def __exit__(self, *args):
                real_lock.release()
        vault._lock = DelayedLock()
    elif expires_during == "directory_lock":
        real_flock = fcntl.flock
        def delayed_flock(*args):
            real_flock(*args)
            expire()
        monkeypatch.setattr(fcntl, "flock", delayed_flock)
    else:
        real_read = key_vault._read_entry
        def delayed_read(*args):
            value = real_read(*args)
            expire()
            return value
        monkeypatch.setattr(key_vault, "_read_entry", delayed_read)
    with pytest.raises(contracts.VaultUnavailable):
        vault.destroy(ref, approval)
    assert entry.read_bytes() == b"synthetic persisted ciphertext"


@linux
@pytest.mark.parametrize("kind", ["local", "key"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_process_death_at_publication_leaves_absent_or_usable_target(tmp_path, kind, boundary):
    """Terminate without finally exactly around the real Linux publication operation."""
    import hashlib
    import multiprocessing
    from app.storage import key_vault, local
    from app.storage.contracts import ExactTargetApproval, KeyRef, StorageConflict, StorageReceipt
    tmp_path.chmod(0o700)
    ref = KeyRef("room", UUID(int=1), UUID(int=2), 1)
    name = str(UUID(int=3)) if kind == "local" else key_vault.SystemdKeyVault.credential_name(ref)
    def child():
        with pytest.MonkeyPatch.context() as patches:
            # Old implementation exposes os.link; corrected one exposes the atomic rename helper.
            target, attribute = (key_vault, "_rename_noreplace") if hasattr(key_vault, "_rename_noreplace") else (key_vault.os, "link")
            operation = getattr(target, attribute)
            def terminate_at_boundary(*args, **kwargs):
                if boundary == "before":
                    os._exit(73)
                operation(*args, **kwargs)
                os._exit(73)
            patches.setattr(target, attribute, terminate_at_boundary)
            if kind == "local":
                local.LocalCiphertextStore(tmp_path).create(UUID(int=3), b"synthetic ciphertext")
            else:
                _synthetic_operator(tmp_path, patches).prepare(ref)
    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("Publication child did not terminate")
    assert process.exitcode == 73
    target = tmp_path / name
    if boundary == "before":
        assert not target.exists()
    else:
        assert target.exists()
    if kind == "local":
        store = local.LocalCiphertextStore(tmp_path)
        if boundary == "before":
            receipt = store.create(UUID(int=3), b"synthetic ciphertext")
        else:
            frame = target.read_bytes()[:20]
            receipt = StorageReceipt("local", name, str(UUID(bytes=frame[4:20])), 20,
                                     hashlib.sha256(b"synthetic ciphertext").digest())
        assert store.read(receipt) == b"synthetic ciphertext"
        assert store.inspect(receipt).ciphertext_size == 20
        with pytest.raises(StorageConflict):
            store.create(UUID(int=3), b"replacement")
        assert store.delete(receipt) == "deleted"
    else:
        with pytest.MonkeyPatch.context() as patches:
            vault = _synthetic_operator(tmp_path, patches)
            if boundary == "before":
                vault.prepare(ref)
            with key_vault._directory(tmp_path, vault._uid) as descriptor:
                raw = key_vault._read_entry(descriptor, name, vault._uid, 65536)
            assert raw.startswith(b"synthetic-host-encrypted:")
            with pytest.raises(StorageConflict):
                vault.prepare(ref)
            approval = ExactTargetApproval(UUID(int=9), ref.domain, ref.owner_id, ref.key_id, 1,
                datetime.now(timezone.utc) + timedelta(minutes=1), UUID(int=8), True)
            vault.destroy(ref, approval)
            assert not target.exists()


@linux
@pytest.mark.parametrize("failure", ["missing_symbol", "ENOSYS", "EOPNOTSUPP"])
def test_publication_requires_native_no_replace_without_fallback(tmp_path, monkeypatch, failure):
    import ctypes
    import errno
    from app.storage.local import LocalCiphertextStore
    from app.storage.contracts import StorageIntegrityError
    tmp_path.chmod(0o700)
    class UnsupportedLibrary:
        pass
    library = UnsupportedLibrary()
    if failure != "missing_symbol":
        def unsupported(*args):
            ctypes.set_errno(getattr(errno, failure))
            return -1
        library.renameat2 = unsupported
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: library)
    with pytest.raises(StorageIntegrityError):
        LocalCiphertextStore(tmp_path).create(UUID(int=3), b"synthetic ciphertext")
    assert list(tmp_path.iterdir()) == []
