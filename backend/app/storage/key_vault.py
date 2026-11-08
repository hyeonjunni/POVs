"""Linux host-encrypted entries: explicit operator provisioning, delivered runtime reads.

Provisioning does not refresh a running service's credential snapshot. Activation,
draining every consumer and independent recovery retention belong to the caller.
No host key is read, no sudo is invoked, and missing entries never regenerate.
"""
from contextlib import contextmanager
import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from uuid import uuid4

from .contracts import ExactTargetApproval, KeyRef, StorageConflict, VaultUnavailable, uuid_value


@contextmanager
def _directory(path, uid):
    """Walk every component with O_NOFOLLOW and retain the verified directory fd."""
    descriptor = None
    try:
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise OSError()
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) not in (0o500, 0o700):
            raise OSError()
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_entry(directory, name, uid, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or not 0 < info.st_size <= limit):
            raise OSError()
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        stable = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                                value.st_ctime_ns, value.st_mode, value.st_uid, value.st_nlink)
        if len(raw) != info.st_size or len(raw) > limit or stable(after) != stable(info):
            raise OSError()
        return bytes(raw)
    finally:
        os.close(descriptor)


def _rename_noreplace(directory, source, target):
    """Linux atomic rename, with no overwrite or unsupported-platform fallback."""
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, "Atomic publication unavailable") from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(source), directory, os.fsencode(target), 1) != 0:  # RENAME_NOREPLACE
        raise OSError(ctypes.get_errno(), "Atomic publication failed")


def _publish(directory, name, value):
    """Fsync then atomic no-replace rename; no two-link interruption window."""
    temporary = ".pending-" + str(uuid4())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _rename_noreplace(directory, temporary, name)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.fsync(directory)


class SystemdKeyVault:
    def __init__(self, *, approved_uid, credentials_directory=None, provisioning_directory=None,
                 operator=False, inventory_revision=None):
        if (sys.platform != "linux" or type(approved_uid) is not int or approved_uid < 0
                or type(operator) is not bool):
            raise VaultUnavailable("Unsupported vault context")
        if operator:
            if os.geteuid() != 0 or provisioning_directory is None or credentials_directory is not None:
                raise VaultUnavailable("Operator context required")
            uuid_value(inventory_revision)
        elif (approved_uid == 0 or os.geteuid() != approved_uid or credentials_directory is None
              or provisioning_directory is not None):
            raise VaultUnavailable("Approved non-root runtime required")
        self._uid = 0 if operator else approved_uid
        self._path = provisioning_directory if operator else credentials_directory
        self._operator = operator
        self._revision = inventory_revision
        self._revoked = set()
        self._lock = threading.RLock()
        try:
            with _directory(self._path, self._uid):
                pass
        except (OSError, ValueError, TypeError):
            raise VaultUnavailable("Vault directory unavailable") from None

    @staticmethod
    def credential_name(ref):
        if type(ref) is not KeyRef:
            raise VaultUnavailable("Invalid key reference")
        return f"ambient-{ref.domain}-{ref.owner_id}-{ref.key_id}-{ref.epoch}"

    def _require_context(self, operator):
        if self._operator != operator or os.geteuid() != (0 if operator else self._uid):
            raise VaultUnavailable("Vault operation not permitted")

    def prepare(self, ref):
        self._require_context(True)
        name = self.credential_name(ref)
        with self._lock:
            if ref in self._revoked:
                raise VaultUnavailable("Key reference revoked")
            try:
                with _directory(self._path, self._uid) as directory:
                    import fcntl
                    fcntl.flock(directory, fcntl.LOCK_EX)
                    try:
                        os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise StorageConflict("Key entry already exists")
                    raw = json.dumps(["ambient-legacy/key", 1, ref.domain, str(ref.owner_id),
                                      str(ref.key_id), ref.epoch, os.urandom(32).hex()], separators=(",", ":")).encode()
                    result = subprocess.run(["/usr/bin/systemd-creds", "encrypt", "--with-key=host",
                        "--name=" + name, "-", "-"], input=raw, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, timeout=30, check=False, env={"PATH": "/usr/bin:/bin"})
                    if result.returncode or not 0 < len(result.stdout) <= 65536:
                        raise VaultUnavailable("Host encryption unavailable")
                    _publish(directory, name, result.stdout)
            except FileExistsError:
                raise StorageConflict("Key entry already exists") from None
            except (OSError, subprocess.SubprocessError):
                raise VaultUnavailable("Key provisioning unavailable") from None

    def load(self, ref):
        self._require_context(False)
        name = self.credential_name(ref)
        with self._lock:
            if ref in self._revoked:
                raise VaultUnavailable("Key reference revoked")
            try:
                with _directory(self._path, self._uid) as directory:
                    raw = _read_entry(directory, name, self._uid, 4096)
                value = json.loads(raw)
                expected = ["ambient-legacy/key", 1, ref.domain, str(ref.owner_id), str(ref.key_id), ref.epoch]
                if (type(value) is not list or len(value) != 7 or value[:6] != expected
                        or type(value[1]) is not int or type(value[5]) is not int
                        or type(value[6]) is not str or len(value[6]) != 64
                        or json.dumps(value, separators=(",", ":")).encode() != raw):
                    raise ValueError()
                key = bytes.fromhex(value[6])
                if len(key) != 32 or key.hex() != value[6]:
                    raise ValueError()
                return key
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                raise VaultUnavailable("Key credential unavailable") from None

    def revoke_cache(self, ref):
        self.credential_name(ref)
        with self._lock:
            self._revoked.add(ref)

    def destroy(self, ref, approval):
        self._require_context(True)
        name = self.credential_name(ref)
        if type(approval) is not ExactTargetApproval:
            raise VaultUnavailable("Exact approval required")
        approval.require(ref, self._revision)
        with self._lock:
            self._revoked.add(ref)
            try:
                with _directory(self._path, self._uid) as directory:
                    import fcntl
                    fcntl.flock(directory, fcntl.LOCK_EX)
                    approval.require(ref, self._revision)
                    _read_entry(directory, name, self._uid, 65536)
                    approval.require(ref, self._revision)
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
            except FileNotFoundError:
                return
            except OSError:
                raise VaultUnavailable("Key destruction unavailable") from None
