"""Demo-only, lossy metadata observations. Never serialize application objects."""
import contextvars
import functools
import json
import math
import os
import socket
import stat
import time
import uuid

_current = contextvars.ContextVar('demo_trace', default=None)
STAGES = {'AUTH', 'QUERY EMBEDDING', 'RETRIEVAL', 'RELEVANCE GATE',
          'ANSWER ROUTER', 'LLM INFERENCE', 'CLAIM VALIDATION', 'CITATION VALIDATION', 'RESPONSE'}
ENUMS = {
    'source_type': {'TEXT'},
    'model': {'bge-m3:latest', 'gemma4:e2b', 'synthetic', 'other'},
    'decision': {'ANSWER', 'NO_RECORD', 'REFUSE', 'CLARIFY', 'CONFLICT', 'ADMIT', 'REJECT'},
    'validation': {'PASS', 'FAIL', 'SKIPPED'},
}
NUMBERS = {'dimension', 'candidate_count', 'cosine', 'bm25', 'floor', 'admitted_evidence', 'citation_count', 'http_status', 'latency_ms'}


def _send(event):
    path = os.environ.get('AMBIENT_DEMO_TRACE_SOCKET', '')
    if not os.path.isabs(path):
        return
    parent = os.lstat(os.path.dirname(path))
    target = os.lstat(path)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISSOCK(target.st_mode) or target.st_uid != os.geteuid()
            or stat.S_IMODE(target.st_mode) != 0o600):
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.sendto(json.dumps(event, allow_nan=False).encode(), path)


def emit(stage, **fields):
    try:
        state = _current.get()
        if state is None or stage not in STAGES:
            return
        event = {'trace_id': state[0], 'stage': stage,
                 'elapsed_ms': round((time.monotonic() - state[1]) * 1000, 3)}
        for key, value in fields.items():
            if key in ENUMS and type(value) is str and value in ENUMS[key]:
                event[key] = value
            elif key in NUMBERS and type(value) in (int, float) and math.isfinite(value):
                event[key] = value
            elif key == 'invoked' and type(value) is bool:
                event[key] = value
            elif key == 'alias' and type(value) is int and 1 <= value <= 51:
                event[key] = f'R{value}'
        _send(event)
    except Exception:
        pass  # No exception text, logging, retries, or effect on Ask.


def observe(stage, fields):
    """Observer failures are isolated, including metadata extraction failures."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                enabled = _current.get() is not None
                started = time.monotonic() if enabled else None
            except Exception:
                enabled = False
            if not enabled:
                return fn(*args, **kwargs)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                emit(stage, validation='FAIL')
                raise
            try:
                emit(stage, latency_ms=(time.monotonic() - started) * 1000,
                     **fields(result, args, kwargs))
            except Exception:
                pass
            return result
        return wrapped
    return decorate


class DemoTraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = None
        try:
            if (os.environ.get('AMBIENT_DEMO_TRACE_SOCKET') and scope['type'] == 'http'
                    and scope.get('method') == 'POST'
                    and scope.get('path', '').endswith('/ask')):
                token = _current.set((uuid.uuid4().hex, time.monotonic()))
        except Exception:
            pass

        async def traced_send(message):
            if message['type'] == 'http.response.start':
                emit('RESPONSE', http_status=message['status'])
            await send(message)
        try:
            await self.app(scope, receive, traced_send)
        finally:
            if token is not None:
                _current.reset(token)
