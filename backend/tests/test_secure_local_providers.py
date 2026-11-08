"""Trusted DGX provider admission; no cloud, no inference in unit tests."""
import asyncio
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import secure_local_providers as local
from app.services.secure_runtime import SecureUnavailable
from app.services.secure_retrieval import synthetic_embedding
from app.ai.grounded_answering import secure_inference_factory
from app.processing.contracts import ProviderError


def config(tmp_path, **changes):
    tmp_path.chmod(0o700)
    return SimpleNamespace(security_mode="SECURE", secure_provider_profile="trusted-dgx-local",
        secure_provider_temp_directory=str(tmp_path), **changes)


def test_default_has_no_real_provider_admission(tmp_path):
    runtime = SimpleNamespace()
    cfg = config(tmp_path)
    cfg.secure_provider_profile = "synthetic"
    local.configure(runtime, cfg)
    assert not hasattr(runtime, "processing_providers")
    assert not hasattr(runtime, "embedding_provider")
    assert not hasattr(runtime, "inference_provider")


@pytest.mark.parametrize("mode", ["LEGACY", "bad"])
def test_local_admission_requires_secure_mode(tmp_path, mode):
    cfg = config(tmp_path); cfg.security_mode = mode
    with pytest.raises(SecureUnavailable):
        local.configure(SimpleNamespace(), cfg)


@pytest.mark.parametrize("unsafe", ["public", "symlink", "missing"])
def test_plaintext_temp_root_fails_closed(tmp_path, unsafe):
    root = tmp_path / "root"
    if unsafe == "public":
        root.mkdir(mode=0o755)
    elif unsafe == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SecureUnavailable):
        local.LocalOCR(root)


def test_real_types_require_explicit_runtime_admission(tmp_path):
    cfg = config(tmp_path)
    runtime = SimpleNamespace()
    local.configure(runtime, cfg)
    assert synthetic_embedding(runtime.embedding_provider, runtime=runtime) is runtime.embedding_provider
    assert secure_inference_factory(runtime)("exaone-45-33b-dgx") is runtime.inference_provider
    runtime.local_provider_admission = False
    with pytest.raises(Exception):
        synthetic_embedding(runtime.embedding_provider, runtime=runtime)
    with pytest.raises(SecureUnavailable):
        secure_inference_factory(runtime)


def test_subclass_cannot_cross_boundary(tmp_path):
    runtime = SimpleNamespace(local_provider_admission=True)
    class Foreign(local.LocalEmbedding): pass
    with pytest.raises(Exception):
        synthetic_embedding(Foreign(), runtime=runtime)


def test_http_worker_has_fixed_endpoint_and_no_proxy_credentials(monkeypatch):
    captured = []
    def run(command, **kw):
        captured.append((command, kw))
        return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b"")
    monkeypatch.setattr(local.subprocess, "run", run)
    monkeypatch.setenv("HTTPS_PROXY", "http://foreign.invalid")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/private/credentials")
    assert local._post("/api/embed", {"input": ["synthetic"]}) == {"ok": True}
    command, kw = captured[0]
    assert "127.0.0.1" in command[-1]
    assert "HTTPConnection" in command[-1]
    assert not any(k in kw["env"] for k in ("HTTPS_PROXY", "HTTP_PROXY", "GOOGLE_APPLICATION_CREDENTIALS"))
    assert kw["timeout"] <= 65
    assert b"foreign.invalid" not in kw["input"]


@pytest.mark.parametrize("raw", [b'{"embeddings":[[1,2]]}', b'not-json'])
def test_bad_embedding_payload_fails_closed(monkeypatch, raw):
    monkeypatch.setattr(local.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd,0,raw,b""))
    with pytest.raises(Exception):
        local.LocalEmbedding().embed(["synthetic"])


def test_http_timeout_is_redacted_and_reaped(monkeypatch):
    def run(cmd, **kw): raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"private text")
    monkeypatch.setattr(local.subprocess, "run", run)
    with pytest.raises(local.LocalProviderUnavailable) as error:
        local._post("/api/embed", {"input":["private text"]})
    assert "private" not in str(error.value)


def test_embedding_model_is_fixed(monkeypatch):
    calls=[]
    def post(path, body):
        calls.append((path,body))
        return {"model":"bge-m3:latest","embeddings":[[1.0]+[0.0]*1023]}
    monkeypatch.setattr(local,"_post",post)
    vector=local.LocalEmbedding().embed(["synthetic"])
    assert len(vector[0])==1024
    assert calls==[("/api/embed", {"model":"bge-m3:latest","input":["synthetic"],"keep_alive":"5m"})]


def test_ocr_timeout_removes_plaintext_and_redacts(tmp_path, monkeypatch):
    from test_processing_providers import ONE_PIXEL_PNG
    cfg=config(tmp_path)
    provider=local.LocalOCR(tmp_path)
    paths=[]
    def run(cmd,**kw):
        paths.append(cmd[1])
        assert cmd[0]=="/usr/bin/tesseract"
        assert kw["shell"] is False
        assert kw["timeout"] <= 30
        raise subprocess.TimeoutExpired(cmd,kw["timeout"],stderr=b"private failure")
    monkeypatch.setattr(local.subprocess,"run",run)
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.extract(ONE_PIXEL_PNG,"image/png"))
    assert error.value.code=="provider_timeout"
    assert "private" not in error.value.message
    assert list(tmp_path.iterdir())==[]
    assert not os.path.exists(paths[0])


def test_http_redirect_is_not_followed(monkeypatch):
    import io
    import http.client
    import sys
    calls=[]
    class Response:
        status=302
        def read(self, size): raise AssertionError("redirect body must not be read")
    class Connection:
        def __init__(self, host, port, timeout):
            assert (host,port)==("127.0.0.1",11434)
        def connect(self): pass
        def request(self,*args): calls.append(args)
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(http.client,"HTTPConnection",Connection)
    monkeypatch.setattr(sys,"stdin", SimpleNamespace(buffer=io.BytesIO(b'["/api/embed",{}]')))
    monkeypatch.setattr(sys,"stdout",io.StringIO())
    with pytest.raises(ValueError):
        exec(local._HTTP_WORKER,{})
    assert len(calls)==1


def test_real_http_child_is_killed_and_reaped_on_deadline(tmp_path, monkeypatch):
    import sys
    pidfile=tmp_path/"pid"
    code="import os,time;open("+repr(str(pidfile))+",'w').write(str(os.getpid()));time.sleep(10)"
    monkeypatch.setattr(local,"_HTTP_WORKER",code)
    original=subprocess.run
    def bounded(cmd,**kw):
        kw["timeout"]=0.2
        return original(cmd,**kw)
    monkeypatch.setattr(local.subprocess,"run",bounded)
    with pytest.raises(local.LocalProviderUnavailable):
        local._post("/api/embed",{})
    assert pidfile.exists()
    assert not os.path.exists("/proc/"+pidfile.read_text())


def test_forbidden_http_path_never_starts_process(monkeypatch):
    runner=Mock(side_effect=AssertionError("network started"))
    monkeypatch.setattr(local.subprocess,"run",runner)
    with pytest.raises(local.LocalProviderUnavailable):
        local._post("http://foreign.invalid",{})
    runner.assert_not_called()


def test_uncertain_http_failstops_without_false_drain_or_unlock(monkeypatch):
    from app.services.secure_runtime import SecureRuntime, RuntimeFailStop
    lock=SimpleNamespace(try_acquire=Mock(return_value=True),release=Mock())
    stops=[]
    runtime=SecureRuntime(lock,check_admission=lambda:None,fail_stop=lambda:stops.append("stop"))
    runtime.local_provider_admission=True
    monkeypatch.setattr(local.subprocess,"run",
        lambda cmd,**kw:subprocess.CompletedProcess(cmd,1,b"",b""))
    async def uncertain():
        async with runtime.operation_guard() as token:
            try:local._post("/api/embed",{})
            finally:assert token.poisoned
    with pytest.raises(RuntimeFailStop):
        asyncio.run(uncertain())
    assert stops==["stop"]
    lock.release.assert_not_called()


def _guarded_post(runtime):
    """Run the real HTTP worker subprocess, retargeted to an owned test endpoint, inside a secure guard."""
    async def call():
        async with runtime.operation_guard():
            local._post("/api/chat", {"messages": [{"role": "user", "content": "synthetic"}]})
    asyncio.run(call())


def _runtime(stops):
    from app.services.secure_runtime import SecureRuntime
    lock = SimpleNamespace(try_acquire=Mock(return_value=True), release=Mock())
    runtime = SecureRuntime(lock, check_admission=lambda: None, fail_stop=lambda: stops.append("stop"))
    runtime.local_provider_admission = True
    return runtime, lock


def test_connect_refused_is_provably_not_delivered_and_does_not_failstop(monkeypatch):
    import socket
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    monkeypatch.setattr(local, "_HTTP_WORKER", local._HTTP_WORKER.replace('"127.0.0.1",11434', f'"127.0.0.1",{port}'))
    stops = []
    runtime, lock = _runtime(stops)
    with pytest.raises(local.LocalProviderUnavailable):
        _guarded_post(runtime)
    assert stops == []
    lock.release.assert_called_once()


def test_owned_slow_provider_timeout_still_failstops_and_keeps_lock(monkeypatch):
    """Fault injection: an owned in-process provider accepts the request and never answers."""
    import socket, threading
    from app.services.secure_runtime import RuntimeFailStop
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1); port = server.getsockname()[1]
    received = []
    def accept():
        connection, _ = server.accept(); received.append(connection.recv(65536)); threading.Event().wait(3); connection.close()
    threading.Thread(target=accept, daemon=True).start()
    worker = local._HTTP_WORKER.replace('"127.0.0.1",11434,timeout=55', f'"127.0.0.1",{port},timeout=0.5')
    assert worker != local._HTTP_WORKER
    monkeypatch.setattr(local, "_HTTP_WORKER", worker)
    stops = []
    runtime, lock = _runtime(stops)
    with pytest.raises(RuntimeFailStop):
        _guarded_post(runtime)
    server.close()
    assert received and b"/api/chat" in received[0]  # the request was delivered, so completion is uncertain
    assert stops == ["stop"]
    lock.release.assert_not_called()


def test_output_reserve_bounds_worst_case_generation_below_provider_deadline():
    from app.ai import context_budget as budget
    worst = ((budget.RUNTIME_NUM_CTX - budget.OUTPUT_RESERVE) / budget.PREFILL_TOKENS_PER_S
             + budget.OUTPUT_RESERVE / budget.DECODE_TOKENS_PER_S)
    assert worst <= 45 < 55
