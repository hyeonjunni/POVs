"""Only public synthetic keys and a fake transport, never real age/export."""
import dataclasses
from datetime import datetime, timezone
from uuid import UUID

import pytest


def manifest():
    from app.storage.contracts import KeyRef
    from app.storage.recovery import RecoveryEntry, RecoveryManifest
    entries = tuple(RecoveryEntry(KeyRef(domain, UUID(int=index), UUID(int=index + 10), 1),
                                  bytes([index]) * 32, "active")
                    for index, domain in enumerate(("room", "user", "lookup"), 1))
    return RecoveryManifest(UUID(int=30), datetime(2026, 9, 12, tzinfo=timezone.utc),
                            UUID(int=31), 1, entries)


def test_recovery_canonical_roundtrip_contains_independent_original_keys():
    from app.storage.recovery import encode_manifest, decode_manifest
    original = manifest()
    encoded = encode_manifest(original)
    assert encoded.startswith(b'["ambient-legacy/recovery",1,"00000000-0000-0000-0000-00000000001e","2026-09-12T00:00:00Z"')
    restored = decode_manifest(encoded)
    assert restored == original
    assert {entry.ref.domain for entry in restored.entries} == {"room", "user", "lookup"}
    assert len({entry.key for entry in restored.entries}) == 3
    assert "01010101" not in repr(restored)


def test_recovery_invalid_canonical_duplicate_revoked_and_limit_rejected():
    from app.storage.recovery import encode_manifest, decode_manifest
    original = manifest()
    encoded = encode_manifest(original)
    for raw in (encoded + b" ", b"{}", encoded.replace(b'",1,"', b'",true,"', 1), b"x" * (1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            decode_manifest(raw)
    for changed in (dataclasses.replace(original, entries=original.entries + original.entries[:1]),
                    dataclasses.replace(original, entries=(dataclasses.replace(original.entries[0], state="revoked"),)),
                    dataclasses.replace(original, recovery_epoch=True)):
        with pytest.raises(ValueError):
            encode_manifest(changed)


def test_fake_transport_rehearsal_rejects_modified_manifest_without_host_dependency():
    from app.storage.recovery import encode_manifest, decode_manifest
    original = manifest()
    class FakeAgeTransport:
        # Deliberately not encryption; a test boundary, never exported by production code.
        def encrypt(self, raw):
            return b"fake-age:" + raw[::-1]
        def decrypt(self, raw):
            if not raw.startswith(b"fake-age:"):
                raise ValueError("Invalid fake transport")
            return raw[9:][::-1]
    transport = FakeAgeTransport()
    restored = decode_manifest(transport.decrypt(transport.encrypt(encode_manifest(original))))
    assert restored == original
    damaged = transport.encrypt(encode_manifest(original))[:-1]
    with pytest.raises(ValueError):
        decode_manifest(transport.decrypt(damaged))
