from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "unit-test-secret-do-not-use-in-prod-xxxx")

import time
from uuid import uuid4

import jwt as pyjwt
import pytest

from app.auth.security import TokenError, decode_access_token
from app.books.bulk import _split_names
from app.config import settings


def test_access_token_rejects_wrong_type():
    payload = {"sub": str(uuid4()), "type": "refresh", "exp": int(time.time()) + 60}
    token = pyjwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(TokenError, match="wrong token type"):
        decode_access_token(token)


def test_split_names_handles_all_separators():
    assert _split_names("A; B ;C") == ["A", "B", "C"]
    assert _split_names("A|B|C") == ["A", "B", "C"]
    assert _split_names(["A", "", "B"]) == ["A", "B"]
    assert _split_names("") == []
    assert _split_names(None) == []
