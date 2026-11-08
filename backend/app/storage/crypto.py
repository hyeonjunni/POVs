"""AES-256-GCM envelopes authenticated against caller-supplied identity."""
import hmac
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from .contracts import HEADER_LIMIT, PayloadIdentity, SealedPayload, StorageIntegrityError, secret_bytes


def encode_aad(identity: PayloadIdentity) -> bytes:
    if type(identity) is not PayloadIdentity:
        raise StorageIntegrityError("Invalid payload identity")
    aad = identity.aad()
    if len(aad) > HEADER_LIMIT:
        raise StorageIntegrityError("Invalid payload header")
    return aad


def seal_payload(identity, plaintext, kek):
    aad = encode_aad(identity)
    secret_bytes(kek, 32)
    if type(plaintext) is not bytes or len(plaintext) != identity.plaintext_length:
        raise StorageIntegrityError("Payload length mismatch")
    dek, nonce = os.urandom(32), os.urandom(12)
    blob = b"ALM4" + struct.pack(">I", len(aad)) + aad + nonce + AESGCM(dek).encrypt(nonce, plaintext, aad)
    return SealedPayload(blob, aes_key_wrap(kek, dek))


def open_payload(identity, blob, wrapped_dek, kek):
    aad = encode_aad(identity)
    secret_bytes(kek, 32)
    secret_bytes(wrapped_dek, 40)
    expected_size = 36 + len(aad) + identity.plaintext_length
    if type(blob) is not bytes or len(blob) != expected_size or blob[:4] != b"ALM4":
        raise StorageIntegrityError("Invalid encrypted payload")
    header_size = struct.unpack(">I", blob[4:8])[0]
    if header_size > HEADER_LIMIT or header_size != len(aad) or not hmac.compare_digest(blob[8:8 + header_size], aad):
        raise StorageIntegrityError("Payload identity mismatch")
    start = 8 + header_size
    try:
        dek = aes_key_unwrap(kek, wrapped_dek)
        return AESGCM(dek).decrypt(blob[start:start + 12], blob[start + 12:], aad)
    except (InvalidTag, InvalidUnwrap, ValueError):
        raise StorageIntegrityError("Payload authentication failed") from None
