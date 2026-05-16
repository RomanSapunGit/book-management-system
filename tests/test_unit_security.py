from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "unit-test-secret")

import time  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from app.auth.security import (  # noqa: E402
    TokenError,
    decode_access_token,
    encode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_roundtrip():
    h = hash_password("hunter2!hunter2!")
    assert verify_password("hunter2!hunter2!", h) is True
    assert verify_password("wrong-password", h) is False


def test_password_verify_handles_corrupt_hash():
    assert verify_password("anything", "not-a-real-argon2-hash") is False


def test_access_token_roundtrip():
    uid = uuid4()
    token, ttl = encode_access_token(uid)
    assert ttl > 0
    assert decode_access_token(token) == uid


def test_access_token_rejects_tampered_signature():
    uid = uuid4()
    token, _ = encode_access_token(uid)
    tampered = token[:-4] + "AAAA"
    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_access_token_rejects_wrong_type():
    import jwt as pyjwt

    from app.config import settings

    payload = {"sub": str(uuid4()), "type": "refresh", "exp": int(time.time()) + 60}
    token = pyjwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(TokenError, match="wrong token type"):
        decode_access_token(token)


def test_refresh_token_high_entropy_and_deterministic_hash():
    a = generate_refresh_token()
    b = generate_refresh_token()
    assert a != b
    assert len(a) >= 32
    assert hash_refresh_token(a) == hash_refresh_token(a)
    assert hash_refresh_token(a) != hash_refresh_token(b)
    assert len(hash_refresh_token(a)) == 64
