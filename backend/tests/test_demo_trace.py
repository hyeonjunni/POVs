import json
import socket
import time

import pytest

from app.core import demo_trace as trace
from test_grounded_answering_api import rig, ask, set_result, index, QUOTE
from test_relevance_admission import hit, admitted, GOOD, BAD


@pytest.mark.parametrize('query,route,invoked', [
    ('아버지 검진은 몇 시였어?', 'ANSWER', True),
    ('오늘 뭐하지', 'NO_RECORD', False),
])
def test_actual_ask_trace_and_unchanged_response(rig, monkeypatch, query, route, invoked):
    tracker, embedding, provider, client = rig
    set_result(tracker)
    index(tracker, embedding)
    baseline = ask(client, query)
    events = []
    monkeypatch.setenv('AMBIENT_DEMO_TRACE_SOCKET', '/unused')
    monkeypatch.setattr(trace, '_send', events.append)
    response = ask(client, query)
    assert response.status_code == baseline.status_code == 200
    assert response.json() == baseline.json()
    assert response.json()['gate_route'] == route
    assert any(e['stage'] == 'LLM INFERENCE' and e.get('invoked') is invoked for e in events)
    assert any(e['stage'] == 'ANSWER ROUTER' and e.get('decision') == route for e in events)
    assert events[-1]['http_status'] == 200
    if invoked:
        assert any(e['stage'] == 'CLAIM VALIDATION' and e.get('validation') == 'PASS' for e in events)
        assert any(e['stage'] == 'CITATION VALIDATION' and e.get('citation_count') == 1 and e['validation'] == 'PASS' for e in events)
    raw = json.dumps(events, ensure_ascii=False)
    for secret in [query, QUOTE, provider.raw, 'room-1', 'record-1', 'claim-1']:
        assert secret not in raw
    assert len({e['trace_id'] for e in events}) == 1

    def broken(_):
        raise RuntimeError('SECRET sink failure')
    monkeypatch.setattr(trace, '_send', broken)
    failed_sink = ask(client, query)
    assert failed_sink.status_code == baseline.status_code
    assert failed_sink.json() == baseline.json()


def test_rejected_alias_and_closed_schema(monkeypatch):
    events = []
    monkeypatch.setattr(trace, '_send', events.append)
    token = trace._current.set(('0' * 32, time.monotonic()))
    try:
        assert len(admitted([hit('stable-a', GOOD, .74, 1.4), hit('stable-b', BAD, .42, 0)])) == 1
        trace.emit('AUTH', query='SECRET', jwt='JWT', credentials='PASSWORD',
                   model='SECRET', decision='SECRET', cosine=float('nan'),
                   embedding=[1, 2], payload={'key': 'KEY'}, validation='PASS')
    finally:
        trace._current.reset(token)
    rejected = next(e for e in events if e.get('decision') == 'REJECT')
    assert rejected['alias'] == 'R2' and rejected['cosine'] == .42
    raw = json.dumps(events, ensure_ascii=False)
    for forbidden in [GOOD, BAD, 'stable-a', 'stable-b', 'SECRET', 'JWT', 'PASSWORD', 'KEY', 'embedding']:
        assert forbidden not in raw
    assert set(events[-1]) == {'trace_id', 'elapsed_ms', 'stage', 'validation'}


def test_private_datagram_and_missing_viewer(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / 'trace.sock'
    monkeypatch.setenv('AMBIENT_DEMO_TRACE_SOCKET', str(path))
    token = trace._current.set(('1' * 32, time.monotonic()))
    try:
        trace.emit('AUTH', validation='PASS')  # absent viewer is harmless
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(str(path))
            path.chmod(0o600)
            receiver.settimeout(.1)
            trace.emit('AUTH', validation='PASS')
            assert json.loads(receiver.recv(8192))['validation'] == 'PASS'
            tmp_path.chmod(0o755)
            trace.emit('AUTH', validation='PASS')
            with pytest.raises(TimeoutError):
                receiver.recv(8192)
    finally:
        trace._current.reset(token)


def test_observer_extraction_failure_preserves_result():
    @trace.observe('RESPONSE', lambda *args: 1 / 0)
    def operation():
        return 'unchanged'
    token = trace._current.set(('2' * 32, time.monotonic()))
    try:
        assert operation() == 'unchanged'
    finally:
        trace._current.reset(token)


from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client
from test_secure_retrieval import processed, index_secure, ask_secure


@pytest.mark.parametrize('query,route,invoked', [
    ('아버지 검진은 몇 시였어?', 'ANSWER', True), ('오늘 뭐하지', 'NO_RECORD', False),
])
def test_secure_real_route_trace(media_client, monkeypatch, query, route, invoked):
    c = media_client
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    baseline = ask_secure(c, query)
    events = []
    monkeypatch.setenv('AMBIENT_DEMO_TRACE_SOCKET', '/unused')
    monkeypatch.setattr(trace, '_send', events.append)
    response = ask_secure(c, query)
    assert response.status_code == baseline.status_code == 200
    assert response.json() == baseline.json()
    assert response.json()['gate_route'] == route
    assert any(e['stage'] == 'LLM INFERENCE' and e.get('invoked') is invoked for e in events)
    assert events[0]['stage'] == 'AUTH'
    assert events[-1]['http_status'] == 200
    raw = json.dumps(events, ensure_ascii=False)
    for forbidden in [query, QUOTE, c.session['access_token'], c.room['room_id'], row['upload_id']]:
        assert forbidden not in raw
    def broken(_):
        raise OSError('private credentials')
    monkeypatch.setattr(trace, '_send', broken)
    response = ask_secure(c, query)
    assert response.status_code == baseline.status_code and response.json() == baseline.json()
