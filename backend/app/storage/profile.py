"""Pure profile crypto and v1 lookup tokens. Tokens never authorize a user."""
from collections.abc import Mapping
import hashlib
import hmac
import json
import re
import struct

from pydantic import EmailStr, TypeAdapter, ValidationError

from .contracts import StorageIntegrityError, positive_int, secret_bytes
from .crypto import open_payload, seal_payload

_EMAIL = TypeAdapter(EmailStr)
_PROVIDERS = {"local_username": "local", "google_subject": "google", "email": "email"}


def normalize_lookup(kind, value):
    if type(value) is not str or not value or len(value) > 16384:
        raise ValueError("Invalid lookup value")
    if kind == "local_username":
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{4,24}", value):
            raise ValueError("Invalid lookup value")
    elif kind == "email":
        try:
            value = str(_EMAIL.validate_python(value)).strip().lower()
        except ValidationError:
            raise ValueError("Invalid lookup value") from None
    elif kind != "google_subject":
        raise ValueError("Invalid lookup kind")
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid lookup value") from None


def blind_index(kind, normalized, key, key_epoch):
    secret_bytes(key, 32)
    positive_int(key_epoch)
    if type(normalized) is not bytes or not normalized or len(normalized) > 65536:
        raise ValueError("Invalid normalized lookup")
    try:
        if normalize_lookup(kind, normalized.decode("utf-8")) != normalized:
            raise ValueError("Lookup is not normalized")
    except UnicodeError:
        raise ValueError("Invalid normalized lookup") from None
    # v1 tuple: each UTF-8 field has a uint32 big-endian byte length; version is ASCII 1.
    fields = [b"ambient-legacy/lookup", b"1", kind.encode(), _PROVIDERS[kind].encode(), normalized]
    message = b"".join(struct.pack(">I", len(value)) + value for value in fields)
    return hmac.digest(key, message, hashlib.sha256)


def _require_user(identity):
    if identity.domain != "user" or identity.artifact_kind not in ("user_profile", "auth_identity"):
        raise ValueError("User-owned profile identity required")


def seal_profile(identity, payload, user_kek):
    _require_user(identity)
    if not isinstance(payload, Mapping):
        raise ValueError("Profile mapping required")
    try:
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid profile payload") from None
    return seal_payload(identity, raw, user_kek)


def open_profile(identity, sealed, user_kek):
    _require_user(identity)
    raw = open_payload(identity, sealed.blob, sealed.wrapped_dek, user_kek)
    try:
        payload = json.loads(raw)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if type(payload) is not dict or canonical != raw:
            raise ValueError()
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StorageIntegrityError("Invalid profile payload") from None
