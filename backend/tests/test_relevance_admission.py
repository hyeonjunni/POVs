from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.ai.gates.answer_router import route_query
from app.ai.gates.entity_index import RoomEntityIndex
from app.ai.grounded_answering import build_fact_sheet, build_answer_plan, render_plan
from app.retrieval.admission import admit_candidates
from app.retrieval.ranking import RankedChunk, fuse_rrf, rank_bm25
from test_grounded_answering import chunk

INDEX = RoomEntityIndex()
QUERY = '아빠 건강검진 몇 시야?'
GOOD = '아버지 건강검진은 10월 20일 오전 9시입니다.'
BAD = '가족방 활성화 중 오류가 발생했습니다.'


def hit(id, text, cosine, bm25=None):
    return RankedChunk(chunk(id, text), 0.02, cosine, bm25, frozenset({'vector'}))


def admitted(hits, query=QUERY):
    return admit_candidates(hits, query, INDEX, vector_floor=0.55)


def route(hits, query=QUERY):
    return route_query(query, [h.chunk.text for h in admitted(hits, query)], INDEX,
                       relevance_admitted=True).route


def test_neighbor_cannot_borrow_relevance_and_exact_citations():
    hits = [hit('r1', GOOD, .7416077249, 1.489787), hit('r2', BAD, .4220108214, 0)]
    query = '아빠 건강검진 언제야?'
    kept = admitted(hits, query)
    assert [h.chunk.id for h in kept] == ['r1']
    assert route(hits, query) == 'ANSWER'
    plan = build_answer_plan(build_fact_sheet([h.chunk for h in kept]), k=10)
    _, citations = render_plan(plan, [c.claim_id for c in plan.claims])
    assert {c['chunk_id'] for c in citations} == {h.chunk.id for h in kept}


def test_weak_nonempty_topk_and_generic():
    hits = [hit('r1', GOOD, .4126889866), hit('r2', BAD, .4239959941)]
    assert route(hits, '오늘 뭐하지') == 'NO_RECORD'
    assert route(hits) == 'NO_RECORD'
    assert route_query('오늘 뭐하지', [GOOD], INDEX).route == 'NO_RECORD'
    assert route([hit('r1', GOOD, .99)], '오늘 뭐하지') == 'NO_RECORD'


@pytest.mark.parametrize('score,expected', [(.549999, False), (.55, True), (.756, True)])
def test_vector_only_paraphrase_threshold(score, expected):
    h = hit('r1', GOOD, score, 0)
    assert bool(admitted([h], '아빠 검진 몇 시야?')) is expected
    if expected:
        assert route([h], '아빠 검진 몇 시야?') == 'ANSWER'


@pytest.mark.parametrize('text', ['아버지 생일은 오전 9시입니다.',
                                  '어머니 건강검진은 10월 20일 오전 9시입니다.'])
def test_explicit_wrong_person_or_event_even_with_strong_scores(text):
    assert not admitted([hit('bad', text, .99, 2)])


def test_related_missing_atom_refuses():
    assert route([hit('r1', '아버지가 건강검진을 예약했다.', .772)]) == 'REFUSE'


def test_multiple_relevant_and_conflict_independent_of_display_depth():
    hits = [hit('r1', GOOD, .8), hit('r2', GOOD, .7), hit('bad', BAD, .42)]
    assert len(admitted(hits)) == 2
    assert route(hits) == 'ANSWER'
    for k in (1, 2, 50):
        plan = build_answer_plan(build_fact_sheet([h.chunk for h in admitted(hits)]), k=k)
        assert len(plan.facts) == min(k, 2)
        assert all(f.source.chunk_id != 'bad' for f in plan.facts)
    hits[1] = hit('r2', GOOD.replace('9시', '11시'), .8)
    assert route(hits) == 'CONFLICT'


@pytest.mark.parametrize('arm', ['bm25', 'vector', 'hybrid'])
def test_all_arms_and_raw_fusion(arm):
    a, b = chunk('a', GOOD), chunk('b', BAD)
    lexical = rank_bm25([a, b], '건강검진은 몇 시야?')
    vector = [RankedChunk(a, .8, cosine=.8, arms=frozenset({'vector'})),
              RankedChunk(b, .42, cosine=.42, arms=frozenset({'vector'}))]
    fused = fuse_rrf([(h.chunk, h.score) for h in lexical], [(h.chunk,h.score) for h in vector])
    assert fused[0].cosine == .8 and fused[0].bm25 > 0
    assert fused[0].arms == frozenset({'bm25', 'vector'})
    hits = {'bm25': lexical, 'vector': vector, 'hybrid': fused}[arm]
    assert [h.chunk.id for h in admitted(hits)] == ['a']


@pytest.mark.parametrize('score', [float('nan'), float('inf'), -.1])
def test_invalid_or_weak_scores_fail_closed(score):
    assert not admitted([hit('bad', BAD, score)])


@pytest.mark.parametrize('arm', ['bm25', 'vector', 'hybrid_rrf'])
@pytest.mark.parametrize('k', [1, 2, 50])
def test_secure_snapshot_filters_before_plan_but_retains_full_validation(monkeypatch, arm, k):
    from app.ai.grounded_answering import load_secure_grounded_snapshot
    from app.schemas.ai import AskRequest
    from app.services import retrieval
    hits = [hit('r1', GOOD, .7416077249, 1.489787), hit('r2', BAD, .4220108214, 0)]
    if arm == 'bm25':
        hits = [replace(h, cosine=None, arms=frozenset({'bm25'})) for h in hits]
    corpus = SimpleNamespace(chunks=tuple(h.chunk for h in hits), member_names=(), record_texts=())
    monkeypatch.setattr(retrieval, 'search_secure_room', lambda **kwargs: (hits, corpus))
    snapshot = load_secure_grounded_snapshot(runtime=object(), user_id='u', room_id='r',
        payload=AskRequest(query=QUERY, model_id='exaone-45-33b-dgx', arm=arm, k=k), candidate_limit=200)
    assert snapshot.route == 'ANSWER'
    assert [f.source.chunk_id for f in snapshot.plan.facts] == ['r1']
    assert {f.source.chunk_id for f in snapshot.facts.facts} == {'r1', 'r2'}
    assert snapshot.catalogue_evidence is corpus


def test_incomplete_neighbor_does_not_get_cited(monkeypatch):
    from app.ai.grounded_answering import load_secure_grounded_snapshot
    from app.schemas.ai import AskRequest
    from app.services import retrieval
    hits = [hit('r1', GOOD, .8), hit('r2', '아버지가 건강검진을 예약했다.', .772)]
    corpus = SimpleNamespace(chunks=tuple(h.chunk for h in hits), member_names=(), record_texts=())
    monkeypatch.setattr(retrieval, 'search_secure_room', lambda **kwargs: (hits, corpus))
    snapshot = load_secure_grounded_snapshot(runtime=object(), user_id='u', room_id='r',
        payload=AskRequest(query=QUERY, model_id='exaone-45-33b-dgx', arm='vector', k=2), candidate_limit=200)
    assert snapshot.route == 'ANSWER'
    assert [f.source.chunk_id for f in snapshot.plan.facts] == ['r1']


def test_weak_unrelated_timed_neighbor_cannot_veto_answer(monkeypatch):
    from app.ai.grounded_answering import load_secure_grounded_snapshot
    from app.schemas.ai import AskRequest
    from app.services import retrieval
    hits = [hit('r1', GOOD, .8, 1), hit('r2', BAD + ' 오후 2시였습니다.', .42, 0)]
    corpus = SimpleNamespace(chunks=tuple(h.chunk for h in hits), member_names=(), record_texts=())
    monkeypatch.setattr(retrieval, 'search_secure_room', lambda **kwargs: (hits, corpus))
    snapshot = load_secure_grounded_snapshot(runtime=object(), user_id='u', room_id='r',
        payload=AskRequest(query=QUERY, model_id='exaone-45-33b-dgx', arm='vector', k=2), candidate_limit=200)
    assert snapshot.route == 'ANSWER'
    assert [f.source.chunk_id for f in snapshot.plan.facts] == ['r1']
