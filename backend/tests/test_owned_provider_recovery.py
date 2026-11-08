"""Owned provider: uncertain completion recovers only after the provider cgroup is proven empty."""
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import secure_local_providers as local
from app.services.secure_runtime import RuntimeFailStop, SecureRuntime, SecureUnavailable

linux = pytest.mark.skipif(sys.platform != "linux", reason="needs /proc and cgroup v2")


def _runtime(stops):
    lock = SimpleNamespace(try_acquire=Mock(return_value=True), release=Mock())
    runtime = SecureRuntime(lock, check_admission=lambda: None, fail_stop=lambda: stops.append("stop"))
    runtime.local_provider_admission = True
    return runtime, lock


def _guarded(runtime, tokens=None):
    async def call():
        async with runtime.operation_guard() as token:
            if tokens is not None:
                tokens.append(token)
            return local._post("/api/chat", {"messages": [{"role": "user", "content": "synthetic"}]})
    return asyncio.run(call())


def _fake_owned(port, cgroup, tied=None):
    owned = object.__new__(local.OwnedProvider)
    owned.port, owned.cgroup = port, Path(cgroup)
    if tied is not None:
        owned.listener = Mock(return_value=tied)
    return owned


def _uncertain(monkeypatch):
    monkeypatch.setattr(local.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b""))


def test_proven_termination_releases_guard_with_503_class_error(monkeypatch):
    owned = _fake_owned(11437, "/sys/fs/cgroup/x", tied=True)
    owned.terminate = Mock(return_value=True)
    monkeypatch.setattr(local, "_owned", owned)
    _uncertain(monkeypatch)
    stops, tokens = [], []
    runtime, lock = _runtime(stops)
    with pytest.raises(local.LocalProviderUnavailable):
        _guarded(runtime, tokens)
    assert stops == [] and not tokens[0].poisoned
    lock.release.assert_called_once()
    owned.terminate.assert_called_once()


def test_unproven_termination_keeps_failstop_and_lock(monkeypatch):
    owned = _fake_owned(11437, "/sys/fs/cgroup/x", tied=True)
    owned.terminate = Mock(return_value=False)
    monkeypatch.setattr(local, "_owned", owned)
    _uncertain(monkeypatch)
    stops, tokens = [], []
    runtime, lock = _runtime(stops)
    with pytest.raises(RuntimeFailStop):
        _guarded(runtime, tokens)
    assert stops == ["stop"] and tokens[0].poisoned
    lock.release.assert_not_called()


def test_no_owned_provider_keeps_existing_failstop(monkeypatch):
    monkeypatch.setattr(local, "_owned", None)
    _uncertain(monkeypatch)
    stops = []
    runtime, lock = _runtime(stops)
    with pytest.raises(RuntimeFailStop):
        _guarded(runtime)
    assert stops == ["stop"]
    lock.release.assert_not_called()


def test_worker_targets_owned_port_only_when_configured(monkeypatch):
    seen = []
    monkeypatch.setattr(local.subprocess, "run",
        lambda cmd, **kw: seen.append(cmd[-1]) or subprocess.CompletedProcess(cmd, 0, b"{}", b""))
    monkeypatch.setattr(local, "_owned", None)
    local._post("/api/embed", {})
    monkeypatch.setattr(local, "_owned", _fake_owned(11437, "/sys/fs/cgroup/x", tied=True))
    local._post("/api/embed", {})
    assert '"127.0.0.1",11434' in seen[0] and '"127.0.0.1",11437' not in seen[0]
    assert '"127.0.0.1",11437' in seen[1] and '"127.0.0.1",11434' not in seen[1]


@pytest.mark.parametrize("tied", [None, False])
def test_untied_owned_daemon_is_not_delivered_and_never_failstops(monkeypatch, tied):
    monkeypatch.setattr(local, "_owned", _fake_owned(11437, "/sys/fs/cgroup/x", tied=tied))
    runner = Mock(side_effect=AssertionError("request sent"))
    monkeypatch.setattr(local.subprocess, "run", runner)
    stops = []
    runtime, lock = _runtime(stops)
    with pytest.raises(local.LocalProviderUnavailable):
        _guarded(runtime)
    runner.assert_not_called()
    assert stops == []
    lock.release.assert_called_once()


def _cfg(tmp_path, **owned):
    tmp_path.chmod(0o700)
    return SimpleNamespace(security_mode="SECURE", secure_provider_profile="trusted-dgx-local",
                           secure_provider_temp_directory=str(tmp_path), **owned)


@pytest.mark.parametrize("owned", [
    dict(secure_owned_provider_port=11434, secure_owned_provider_cgroup="/sys/fs/cgroup"),  # shared/production port
    dict(secure_owned_provider_port=11437, secure_owned_provider_cgroup="/tmp"),            # not a cgroup
    dict(secure_owned_provider_port=11437, secure_owned_provider_cgroup="/sys/fs/cgroup/../../tmp"),
    dict(secure_owned_provider_port=11437, secure_owned_provider_cgroup=""),                # half configured
    dict(secure_owned_provider_port=0, secure_owned_provider_cgroup="/sys/fs/cgroup"),
])
def test_bad_owned_settings_fail_closed(tmp_path, monkeypatch, owned):
    monkeypatch.setattr(local, "_owned", None)
    with pytest.raises(SecureUnavailable):
        local.configure(SimpleNamespace(), _cfg(tmp_path, **owned))
    assert local._owned is None


def test_owned_settings_refused_for_synthetic_and_retargeted_worker(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, secure_owned_provider_port=11437, secure_owned_provider_cgroup="/sys/fs/cgroup")
    cfg.secure_provider_profile = "synthetic"
    with pytest.raises(SecureUnavailable):
        local.configure(SimpleNamespace(), cfg)
    cfg.secure_provider_profile = "trusted-dgx-local"
    monkeypatch.setattr(local, "_HTTP_WORKER", local._HTTP_WORKER.replace("11434", "11436"))
    with pytest.raises(SecureUnavailable):
        local.configure(SimpleNamespace(), cfg)


@linux
def test_listener_outside_cgroup_is_not_killed(tmp_path):
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1)
    cgroup = tmp_path / "cg"; cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text(f"{os.getppid()}\n")  # not the listener's pid
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    try:
        assert _fake_owned(server.getsockname()[1], cgroup).terminate(deadline=0.2) is False
    finally:
        server.close()
    assert not (cgroup / "cgroup.kill").exists()


@linux
def test_listener_inside_cgroup_is_killed_then_waits_for_empty(tmp_path):
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1)
    cgroup = tmp_path / "cg"; cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text(f"{os.getpid()}\n")
    (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    owned = _fake_owned(server.getsockname()[1], cgroup)
    try:
        assert owned.terminate(deadline=0.2) is False       # still populated: no proof
        assert (cgroup / "cgroup.kill").read_text() == "1"
        (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        assert owned.terminate(deadline=0.2) is True
    finally:
        server.close()


@linux
def test_vanished_listener_still_needs_and_gets_cgroup_proof(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]  # nothing listens now
    cgroup = tmp_path / "cg"; cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    assert _fake_owned(port, cgroup).terminate(deadline=0.2) is True
    assert (cgroup / "cgroup.kill").read_text() == "1"
    assert _fake_owned(port, tmp_path / "removed").terminate(deadline=0.2) is True  # rmdir needs an empty cgroup


@linux
def test_missing_or_unwritable_control_files_in_existing_cgroup_are_no_proof(tmp_path, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]  # no listener: proof rests on the cgroup alone
    cgroup = tmp_path / "cg"; cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    owned = _fake_owned(port, cgroup)
    assert owned.terminate(deadline=0.2) is False                  # cgroup.events missing, directory present
    (cgroup / "cgroup.kill").unlink()
    real_write = Path.write_text
    def no_kill_file(self, *args, **kwargs):
        if self.name == "cgroup.kill":
            raise FileNotFoundError(self)
        return real_write(self, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", no_kill_file)
    assert owned.terminate(deadline=0.2) is False                  # cgroup.kill missing, directory present
    monkeypatch.setattr(Path, "write_text", real_write)
    (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    (cgroup / "cgroup.kill").write_text(""); (cgroup / "cgroup.kill").chmod(0o400)
    if os.geteuid() != 0:
        assert owned.terminate(deadline=0.2) is False              # permission error is never proof
    (cgroup / "cgroup.kill").chmod(0o600)
    assert owned.terminate(deadline=0.2) is True                   # control: kill written, populated 0


# Real kernel proof: a transient systemd user unit is the owned provider; it accepts the
# request and never answers. cgroup.kill + populated 0 must precede release, and the next
# request must succeed against a fresh provider.
_HANG = ("import socket,time\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
         "s.bind(('127.0.0.1',{port}));s.listen(4)\nwhile True:\n c,_=s.accept();c.recv(65536);time.sleep(3600)\n")
_CRASH = ("import socket,os\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
          "s.bind(('127.0.0.1',{port}));s.listen(4)\nc,_=s.accept();c.recv(65536);os._exit(1)\n")
_ANSWER = ("import socket,json\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
           "s.bind(('127.0.0.1',{port}));s.listen(4)\nwhile True:\n c,_=s.accept();c.recv(65536)\n"
           " b=json.dumps({{'ok':True}}).encode()\n"
           " c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: '+str(len(b)).encode()+b'\\r\\n\\r\\n'+b);c.close()\n")


def _start_unit(name, code):
    subprocess.run(["systemd-run", "--user", "--quiet", f"--unit={name}", "--property=Type=exec",
                    sys.executable, "-I", "-c", code], check=True, timeout=10)
    out = subprocess.run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", name],
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path("/sys/fs/cgroup" + out)


def _wait_listen(port, cgroup):
    # Poll the kernel table, not a connection: a probe would consume the fake provider's accept.
    for _ in range(200):
        if local.OwnedProvider(port, str(cgroup)).listener() is True:
            return
        time.sleep(0.02)
    raise AssertionError("fake provider did not listen")


@linux
@pytest.mark.skipif(shutil.which("systemd-run") is None or not os.environ.get("XDG_RUNTIME_DIR"),
                    reason="needs a systemd user manager")
def test_real_cgroup_timeout_recovery_then_next_request_succeeds(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    names = [f"ambient-owned-provider-test-{uuid.uuid4().hex[:12]}" for _ in range(3)]
    try:
        cgroup = _start_unit(names[0], _HANG.format(port=port))
        _wait_listen(port, cgroup)
        main_pid = int((cgroup / "cgroup.procs").read_text().split()[0])
        monkeypatch.setattr(local, "_HTTP_WORKER", local._HTTP_WORKER.replace("timeout=55", "timeout=0.5"))
        monkeypatch.setattr(local, "_owned", local.OwnedProvider(port, str(cgroup)))
        stops, tokens = [], []
        runtime, lock = _runtime(stops)
        started = time.monotonic()
        with pytest.raises(local.LocalProviderUnavailable):
            _guarded(runtime, tokens)
        assert time.monotonic() - started < 5
        assert stops == [] and not tokens[0].poisoned
        lock.release.assert_called_once()
        assert not os.path.exists(f"/proc/{main_pid}")
        assert not cgroup.exists() or "populated 0" in (cgroup / "cgroup.events").read_text()

        # Provider down: connect refused is not delivered, still no fail-stop.
        with pytest.raises(local.LocalProviderUnavailable):
            _guarded(runtime)
        # Daemon crash after receiving the request: the listener vanishes, the cgroup proof still holds.
        cgroup = _start_unit(names[1], _CRASH.format(port=port))
        crashed = local.OwnedProvider(port, str(cgroup))
        proofs = []
        crashed.terminate = lambda real=crashed.terminate: proofs.append(real()) or proofs[-1]
        monkeypatch.setattr(local, "_owned", crashed)
        _wait_listen(port, cgroup)
        with pytest.raises(local.LocalProviderUnavailable):
            _guarded(runtime)
        assert proofs == [True] and stops == []  # delivered, then proven drained (not the not-delivered path)
        # Fresh provider: the next question is answered through the same runtime.
        cgroup = _start_unit(names[2], _ANSWER.format(port=port))
        monkeypatch.setattr(local, "_owned", local.OwnedProvider(port, str(cgroup)))
        _wait_listen(port, cgroup)
        assert _guarded(runtime) == {"ok": True}
        assert stops == [] and lock.release.call_count == 4
    finally:
        for name in names:
            subprocess.run(["systemctl", "--user", "stop", name], capture_output=True, timeout=15)
            subprocess.run(["systemctl", "--user", "reset-failed", name], capture_output=True, timeout=15)
