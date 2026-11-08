import pytest
from sqlalchemy import text

from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client, record, upload, content_path


@pytest.mark.parametrize('active,deleted', [(1, 0), (0, 1), (1, 1)])
@pytest.mark.parametrize('with_file', [False, True])
def test_list_excludes_real_deletion_before_resolution(media_client, monkeypatch, active, deleted, with_file):
    from app.api.routes import secure_media as media
    c = media_client
    make = upload if with_file else record
    live = [make(c) for _ in range(active)]
    removed = [make(c) for _ in range(deleted)]
    retained_objects = dict(c.objects)
    for row in removed:
        response = c.request('DELETE', '/uploads/' + row['upload_id'], headers=c.auth)
        assert response.status_code == 200, response.text
        with c.engine.connect() as db:
            assert db.scalar(text("SELECT state FROM protected_pointers WHERE owner_type='record' AND owner_id=:id"), {'id': row['upload_id']}) == 'ENCRYPTED_ACTIVE'
            assert db.scalar(text("SELECT count(*) FROM security_tombstones WHERE domain='room' AND owner_id=:r AND target_kind='record' AND target_id=:id"), {'r': c.room['room_id'], 'id': row['upload_id']}) == 0
            state = db.execute(text("SELECT lifecycle_state,soft_deleted_at FROM records WHERE id=:id"), {'id': row['upload_id']}).one()
            assert state[0] == 'SOFT_DELETED' and state[1] is not None
        path = content_path(row) if with_file else '/uploads/' + row['upload_id'] + '/content'
        direct = c.request('GET', path, headers=c.auth)
        assert direct.status_code == 404
        assert direct.json() == {'detail': {'code': 'RESOURCE_NOT_AVAILABLE'}}
    assert c.objects == retained_objects

    seen = []
    original = media.record_source
    def tracked(db, uid, oid):
        seen.append(oid)
        assert oid not in {row['upload_id'] for row in removed}
        return original(db, uid, oid)
    monkeypatch.setattr(media, 'record_source', tracked)
    response = c.request('GET', '/uploads/' + c.room['room_id'], headers=c.auth)
    assert response.status_code == 200, response.text
    expected = {row['upload_id'] for row in live}
    assert {row['upload_id'] for row in response.json()} == expected
    assert set(seen) == expected


@pytest.mark.parametrize('fault', ['object', 'ciphertext'])
def test_non_deleted_corruption_remains_visible(media_client, fault):
    c = media_client
    row = upload(c)
    with c.engine.begin() as db:
        if fault == 'object':
            db.execute(text("UPDATE encrypted_objects SET state='RETIRED' WHERE record_id=:id"), {'id': row['upload_id']})
        else:
            oid = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='record' AND owner_id=:id"), {'id': row['upload_id']})
            receipt, blob = c.objects[oid]
            c.objects[oid] = (receipt, bytes([blob[0] ^ 1]) + blob[1:])
    response = c.request('GET', '/uploads/' + c.room['room_id'], headers=c.auth)
    assert response.status_code == 503
    assert response.json() == {'detail': 'secure_request_unavailable'}


def test_nonmember_cannot_list_even_tombstoned_room(media_client, monkeypatch):
    from app.api.routes import secure_media as media
    c = media_client
    row = upload(c)
    assert c.request('DELETE', '/uploads/' + row['upload_id'], headers=c.auth).status_code == 200
    outsider, _, _ = c.signup('outsider', 'outsider@example.com')
    seen = []
    monkeypatch.setattr(media, 'record_source', lambda *args: seen.append(args))
    response = c.request('GET', '/uploads/' + c.room['room_id'],
                         headers={'Authorization': 'Bearer ' + outsider['access_token']})
    assert response.status_code == 503
    assert response.json() == {'detail': 'secure_request_unavailable'}
    assert seen == []
