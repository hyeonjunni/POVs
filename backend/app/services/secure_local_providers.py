"""Opt-in, fixed DGX providers. No URL/model/executable supplied by clients.

Only decrypted bytes/text enter these local processes. HTTP workers have a
hard process deadline, no proxy/credentials, and never follow redirects.
Synchronous calls finish (or subprocess.run kills/reaps) before guard release.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from app.processing.contracts import ProviderError
from app.processing.ocr_preprocessing import prepare_ocr_image
from app.retrieval.embeddings import validate_embeddings
from app.services.secure_runtime import SecureUnavailable

PROFILE = "trusted-dgx-local"
INFERENCE_MODEL = "gemma4:31b"
MAX_RESPONSE = 4 * 1024 * 1024


class LocalProviderUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Local processing is unavailable.")


# No environment proxy lookup, DNS, redirects or configurable endpoint.
# The worker cannot spawn children; the parent kills and reaps on deadline.
_HTTP_WORKER = """
import http.client,json,sys
path,body=json.loads(sys.stdin.buffer.read(1048577))
if path not in ("/api/embed","/api/chat"): raise ValueError()
connection=http.client.HTTPConnection("127.0.0.1",11434,timeout=55)
try:
    try: connection.connect()
    except OSError: sys.exit(3)
    connection.request("POST",path,json.dumps(body),{"Content-Type":"application/json"})
    response=connection.getresponse()
    if response.status != 200: raise ValueError()
    raw=response.read(4194305)
    if len(raw)>4194304: raise ValueError()
    value=json.loads(raw)
    if not isinstance(value,dict): raise ValueError()
    sys.stdout.write(json.dumps(value))
finally:
    connection.close()
"""


class OwnedProvider:
    """Operator-owned provider unit (opt-in). Its whole cgroup holds only that daemon.

    On uncertain completion the cgroup is killed and proven empty before the guard
    is released; any doubt keeps the fail-stop. Never configured for a shared daemon.
    """
    def __init__(self, port, cgroup):
        path = Path(cgroup) if isinstance(cgroup, str) else None
        if (type(port) is not int or not 1024 <= port <= 65535 or port == 11434 or path is None
                or not path.is_absolute() or ".." in path.parts or path.parts[:4] != ("/", "sys", "fs", "cgroup")):
            raise SecureUnavailable()
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise SecureUnavailable()
        except OSError:
            raise SecureUnavailable() from None
        self.port, self.cgroup = port, path

    def listener(self):
        """True: the single loopback listener on the port belongs to this cgroup. None: nothing listens.
        False: a foreign/ambiguous listener or unreadable state."""
        wanted = "0100007F:%04X" % self.port
        try:
            with open("/proc/net/tcp") as table:
                sockets = {f"socket:[{f[9]}]" for f in (line.split() for line in list(table)[1:])
                           if f[1] == wanted and f[3] == "0A"}
            if not sockets:
                return None
            if len(sockets) != 1:
                return False
            for pid in (self.cgroup / "cgroup.procs").read_text().split():
                try:
                    fds = os.listdir(f"/proc/{pid}/fd")
                except OSError:
                    continue
                for fd in fds:
                    try:
                        if os.readlink(f"/proc/{pid}/fd/{fd}") in sockets:
                            return True
                    except OSError:
                        pass
        except (OSError, ValueError, IndexError):
            pass
        return False

    def terminate(self, deadline=10.0):
        """True only when every task of the provider cgroup has exited (kernel `populated 0`).

        _post tied the request to this cgroup before sending. A listener that vanished since
        (daemon crash) still needs the cgroup proof; a foreign listener never gets one.
        """
        import time
        if self.listener() is False:
            return False
        try:
            (self.cgroup / "cgroup.kill").write_text("1")
            end = time.monotonic() + deadline
            while time.monotonic() < end:
                if "populated 0" in (self.cgroup / "cgroup.events").read_text().splitlines():
                    return True
                time.sleep(0.005)
        except FileNotFoundError:
            return self._removed()  # a missing control file inside an existing directory is no proof
        except (OSError, ValueError):
            pass
        return False

    def _removed(self):
        # Only the cgroup directory itself being absent proves emptiness: the kernel refuses rmdir
        # of a cgroup with tasks or children. A recreated directory (restart) fails closed.
        try:
            os.lstat(self.cgroup)
        except FileNotFoundError:
            return True
        except OSError:
            pass
        return False


_owned = None  # Set only by configure(); every _post then targets the same owned daemon.
_LOOPBACK = '"127.0.0.1",11434'


def _uncertain_http():
    # Killing the HTTP worker cannot prove the shared daemon stopped inference.
    # Do not acknowledge drain or release this admission generation on uncertainty.
    from app.services.secure_runtime import _current_guard, RuntimeFailStop
    guard = _current_guard.get()
    if guard is not None and getattr(guard.runtime, "local_provider_admission", False) is True:
        if type(_owned) is OwnedProvider and _owned.terminate():
            # Worker reaped and provider cgroup empty: provably drained, same as not delivered.
            raise LocalProviderUnavailable() from None
        guard.poisoned = True
        guard.runtime.fail_stop()
        raise RuntimeFailStop()
    raise LocalProviderUnavailable() from None


class _NotDelivered(Exception):
    pass


# The worker exits with this code only when the TCP connect failed, so no request byte (no plaintext)
# reached the daemon. That one case is provably not in flight; every other failure stays uncertain.
_NOT_DELIVERED = 3


def _post(path, body):
    if path not in ("/api/embed", "/api/chat"):
        raise LocalProviderUnavailable()
    raw = json.dumps([path, body], allow_nan=False).encode()
    if len(raw) > 1024 * 1024:
        raise LocalProviderUnavailable()
    if _owned is not None and _owned.listener() is not True:
        # Nothing sent: the owned daemon is not serving yet (restart/warm-up) or the port is not provably its.
        # ponytail: a foreign process binding the port in the ms before connect is not excluded (same-uid scope).
        raise LocalProviderUnavailable()
    try:
        # ponytail: bounded synchronous call holds the secure guard up to 65s;
        # an owned async transport is needed only if concurrent throughput matters.
        worker = _HTTP_WORKER if _owned is None else _HTTP_WORKER.replace(_LOOPBACK, f'"127.0.0.1",{_owned.port}')
        result = subprocess.run([sys.executable, "-I", "-c", worker],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=65, check=False, env={"PATH": "/usr/bin:/bin"})
        if result.returncode == _NOT_DELIVERED:
            raise _NotDelivered()
        if result.returncode != 0 or len(result.stdout) > MAX_RESPONSE:
            raise LocalProviderUnavailable()
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise LocalProviderUnavailable()
        return payload
    except _NotDelivered:
        raise LocalProviderUnavailable() from None
    except (LocalProviderUnavailable, OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        _uncertain_http()


def _private_root(root):
    path = Path(root)
    try:
        info = path.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise SecureUnavailable()
    except OSError:
        raise SecureUnavailable() from None
    return path


class LocalOCR:
    extractor_id = "trusted_dgx_tesseract"

    def __init__(self, root):
        self.root = _private_root(root)

    async def extract(self, content, mime_type):
        from app.core.config import settings
        from app.processing.local_ocr import _parse, _MIME_SUFFIXES
        _private_root(self.root)
        if type(content) is not bytes or len(content) > 20 * 1024 * 1024 or mime_type not in _MIME_SUFFIXES:
            raise ProviderError("provider_input", "Processing input is invalid.")
        try:
            binary = Path("/usr/bin/tesseract").stat()
            if binary.st_uid not in (0, os.geteuid()) or binary.st_mode & 0o022:
                raise OSError()
            with tempfile.TemporaryDirectory(prefix="ocr-", dir=self.root) as raw:
                directory = Path(raw)
                source = directory / "input.png"
                output = directory / "output"
                working = prepare_ocr_image(content, source)
                # No await/thread: cancellation cannot release the guard while
                # subprocess still owns plaintext. Hard timeout kills/reaps.
                result = subprocess.run(["/usr/bin/tesseract", str(source), str(output),
                    "-l", "kor+eng", "--psm", str(settings.ocr_psm), "tsv", "txt"], shell=False, timeout=30,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"})
                if result.returncode != 0:
                    raise ProviderError("provider_failure", "Processing failed.")
                tsv, txt = output.with_suffix(".tsv"), output.with_suffix(".txt")
                if tsv.stat().st_size + txt.stat().st_size > settings.processing_max_output_bytes:
                    raise ProviderError("provider_output", "Processing output is invalid.")
                return working.normalized_regions(_parse(tsv, txt, "kor+eng"))
        except subprocess.TimeoutExpired:
            raise ProviderError("provider_timeout", "Processing timed out.") from None
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("provider_failure", "Processing failed.") from None


class LocalEmbedding:
    model_key = "bge-m3:latest"
    dimensions = 1024

    def embed(self, texts):
        if not texts:
            return []
        if any(not isinstance(t, str) for t in texts):
            raise LocalProviderUnavailable()
        result = _post("/api/embed", {"model":"bge-m3:latest", "input":list(texts), "keep_alive":"5m"})
        if result.get("model") != "bge-m3:latest":
            raise LocalProviderUnavailable()
        return validate_embeddings(result.get("embeddings"), expected_count=len(texts), dimensions=1024)


class LocalInference:
    def generate(self, request):
        from app.ai.providers.ollama_provider import OllamaProvider
        from app.ai.providers.base import InferenceResponse
        from app.ai.context_budget import OUTPUT_RESERVE, RUNTIME_NUM_CTX
        payload = OllamaProvider().build_payload(request)
        payload.update(model=INFERENCE_MODEL, format="json", think=False,
            options={"temperature":0, "num_predict":256, "num_ctx":RUNTIME_NUM_CTX}, keep_alive="5m")
        if request.prompt_package.get("response_mode") == "record_answer":
            payload.pop("format")
            payload["options"]["num_predict"] = OUTPUT_RESERVE
        result = _post("/api/chat", payload)
        if result.get("model") != INFERENCE_MODEL:
            raise LocalProviderUnavailable()
        message = result.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LocalProviderUnavailable()
        tokens = result.get("prompt_eval_count")
        return InferenceResponse("ollama-trusted-dgx", request.model_id, message["content"], "local",
                                 done_reason=result.get("done_reason") if isinstance(result.get("done_reason"), str) else None,
                                 prompt_tokens=tokens if type(tokens) is int else None)


def configure(runtime, config):
    global _owned
    _owned = None
    port = getattr(config, "secure_owned_provider_port", 0)
    cgroup = getattr(config, "secure_owned_provider_cgroup", "")
    if config.secure_provider_profile == "synthetic":
        if port or cgroup:
            raise SecureUnavailable()
        return
    if config.security_mode != "SECURE" or config.secure_provider_profile != PROFILE:
        raise SecureUnavailable()
    if port or cgroup:
        if _HTTP_WORKER.count(_LOOPBACK) != 1:
            raise SecureUnavailable()  # A retargeted worker could not be tied to this proof.
        owned = OwnedProvider(port, cgroup)
    else:
        owned = None
    ocr = LocalOCR(config.secure_provider_temp_directory)
    runtime.processing_providers = {"ocr": ocr}
    runtime.embedding_provider = LocalEmbedding()
    runtime.inference_provider = LocalInference()
    runtime.local_provider_admission = True
    _owned = owned
