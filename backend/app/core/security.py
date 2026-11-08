import base64
import hashlib
import hmac
import random
import secrets
import string
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import settings


def generate_invite_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390000)
    return "pbkdf2_sha256${iterations}${salt}${hash}".format(
        iterations=390000,
        salt=base64.b64encode(salt).decode("ascii"),
        hash=base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False

    try:
        algorithm, iterations_text, encoded_salt, encoded_hash = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(encoded_salt.encode("ascii"))
        expected_hash = base64.b64decode(encoded_hash.encode("ascii"))
    except Exception:
        return False

    candidate_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate_hash, expected_hash)


def create_access_token(user_id: str) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expires_minutes)
    payload = {
        "sub": user_id,
        "exp": expires_at,
        "iat": datetime.now(UTC),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"],
                         options={"require": ["sub", "exp", "iat", "type"]})
    subject = payload["sub"]
    if (type(subject) is not str or not subject or subject != subject.strip()
            or any(character.isspace() or ord(character) < 32 for character in subject)
            or type(payload["exp"]) is not int or type(payload["iat"]) is not int
            or not 0 < payload["iat"] < payload["exp"] or payload["type"] != "access"):
        raise jwt.InvalidTokenError("Invalid access token claims")
    return payload
