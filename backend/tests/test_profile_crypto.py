import dataclasses
import json
from uuid import UUID

import pytest


def test_profile_roundtrip_user_boundary_and_secret_repr():
    from app.storage.contracts import PayloadIdentity, StorageIntegrityError
    from app.storage.profile import seal_profile, open_profile
    payload = {"name": "Synthetic", "email": "synthetic@example.com"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    identity = PayloadIdentity("user", UUID(int=1), None, UUID(int=2), 1, "user_profile", None, None, len(raw))
    sealed = seal_profile(identity, payload, bytes(32))
    assert open_profile(identity, sealed, bytes(32)) == payload
    assert "Synthetic" not in repr(sealed)
    with pytest.raises(StorageIntegrityError):
        open_profile(dataclasses.replace(identity, owner_id=UUID(int=3)), sealed, bytes(32))
    with pytest.raises(ValueError):
        seal_profile(dataclasses.replace(identity, domain="room", artifact_kind="room_metadata"), payload, bytes(32))


@pytest.mark.parametrize("kind,value,expected", [
    ("local_username", "  Abcd._-  ", b"Abcd._-"),
    ("google_subject", " Subject ", b" Subject "),
    ("email", "TEST@Example.com", b"test@example.com")])
def test_lookup_normalization_preserves_existing_semantics(kind, value, expected):
    from app.storage.profile import normalize_lookup
    assert normalize_lookup(kind, value) == expected


@pytest.mark.parametrize("kind,value", [("local_username", "abc"), ("local_username", "abcd/ef"),
    ("local_username", "홍길동12"), ("email", "not-email"), ("google_subject", ""), ("other", "abcd")])
def test_invalid_lookup_rejected(kind, value):
    from app.storage.profile import normalize_lookup
    with pytest.raises(ValueError):
        normalize_lookup(kind, value)


def test_lookup_domain_key_separation_and_explicit_epoch():
    from app.storage.profile import blind_index, normalize_lookup
    value = normalize_lookup("local_username", "Abcd")
    token = blind_index("local_username", value, bytes(32), 1)
    assert len(token) == 32
    assert token != blind_index("google_subject", value, bytes(32), 1)
    assert token != blind_index("local_username", value, bytes([1]) * 32, 2)
    # Epoch is catalogue metadata, not silently a new normalization/message format.
    assert token == blind_index("local_username", value, bytes(32), 2)
    with pytest.raises(ValueError):
        blind_index("local_username", value, bytes(32), True)
    with pytest.raises(ValueError):
        blind_index("local_username", b" Abcd ", bytes(32), 1)
