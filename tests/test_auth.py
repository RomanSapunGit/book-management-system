from __future__ import annotations

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


_USER = {"email": "bob@example.com", "password": "correct-horse-battery-staple"}


async def test_register_then_login(client):
    r = await client.post("/auth/register", json=_USER)
    assert r.status_code == 201
    assert r.json()["email"] == _USER["email"]

    r = await client.post("/auth/login", json=_USER)
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


async def test_register_duplicate_email_409(client):
    await client.post("/auth/register", json=_USER)
    r = await client.post("/auth/register", json=_USER)
    assert r.status_code == 409

    r = await client.post("/auth/register", json={"email": _USER["email"].upper(), "password": _USER["password"]})
    assert r.status_code == 409


async def test_login_wrong_password_401(client):
    await client.post("/auth/register", json=_USER)
    r = await client.post("/auth/login", json={"email": _USER["email"], "password": "wrong-password-but-valid-length"})
    assert r.status_code == 401


async def test_login_unknown_email_401(client):
    r = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 12})
    assert r.status_code == 401


async def test_access_token_works(client):
    await client.post("/auth/register", json=_USER)
    r = await client.post("/auth/login", json=_USER)
    token = r.json()["access_token"]
    r = await client.post("/authors", json={"name": "T"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201


async def test_bad_access_token_401(client):
    r = await client.post("/authors", json={"name": "T"}, headers={"Authorization": "Bearer not.a.real.jwt"})
    assert r.status_code == 401


async def test_refresh_rotates(client):
    await client.post("/auth/register", json=_USER)
    r = await client.post("/auth/login", json=_USER)
    pair1 = r.json()
    r = await client.post("/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    assert r.status_code == 200
    pair2 = r.json()
    assert pair2["refresh_token"] != pair1["refresh_token"]
    r = await client.post(
        "/authors",
        json={"name": "Post-rotation Author"},
        headers={"Authorization": f"Bearer {pair2['access_token']}"},
    )
    assert r.status_code == 201

    r = await client.post("/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    assert r.status_code == 401


async def test_refresh_reuse_revokes_all_sessions(client):
    await client.post("/auth/register", json=_USER)
    pair_a = (await client.post("/auth/login", json=_USER)).json()
    pair_b = (await client.post("/auth/login", json=_USER)).json()

    new_a = (await client.post("/auth/refresh", json={"refresh_token": pair_a["refresh_token"]})).json()

    r = await client.post("/auth/refresh", json={"refresh_token": pair_a["refresh_token"]})
    assert r.status_code == 401

    r = await client.post("/auth/refresh", json={"refresh_token": pair_b["refresh_token"]})
    assert r.status_code == 401
    r = await client.post("/auth/refresh", json={"refresh_token": new_a["refresh_token"]})
    assert r.status_code == 401


async def test_logout_revokes_only_target(client):
    await client.post("/auth/register", json=_USER)
    pair_a = (await client.post("/auth/login", json=_USER)).json()
    pair_b = (await client.post("/auth/login", json=_USER)).json()

    r = await client.post("/auth/logout", json={"refresh_token": pair_a["refresh_token"]})
    assert r.status_code == 204

    r = await client.post("/auth/refresh", json={"refresh_token": pair_a["refresh_token"]})
    assert r.status_code == 401
    r = await client.post("/auth/refresh", json={"refresh_token": pair_b["refresh_token"]})
    assert r.status_code == 200


async def test_logout_idempotent(client):
    await client.post("/auth/register", json=_USER)
    pair = (await client.post("/auth/login", json=_USER)).json()
    r = await client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 204
    r = await client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 204
    r = await client.post("/auth/logout", json={"refresh_token": "not-a-real-token"})
    assert r.status_code == 204


async def test_logout_all_revokes_every_session(client):
    await client.post("/auth/register", json=_USER)
    pair_a = (await client.post("/auth/login", json=_USER)).json()
    pair_b = (await client.post("/auth/login", json=_USER)).json()

    headers = {"Authorization": f"Bearer {pair_a['access_token']}"}
    r = await client.post("/auth/logout-all", headers=headers)
    assert r.status_code == 204

    for pair in (pair_a, pair_b):
        r = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert r.status_code == 401
