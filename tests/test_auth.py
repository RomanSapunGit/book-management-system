from __future__ import annotations

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


_USER = {"email": "alice@example.com", "password": "correct-horse-battery-staple"}


async def test_refresh_rotates_and_old_token_dies(client):
    await client.post("/auth/register", json=_USER)
    pair1 = (await client.post("/auth/login", json=_USER)).json()
    pair2 = (
        await client.post("/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    ).json()
    assert pair2["refresh_token"] != pair1["refresh_token"]

    r = await client.post(
        "/authors",
        json={"name": "T"},
        headers={"Authorization": f"Bearer {pair2['access_token']}"},
    )
    assert r.status_code == 201

    r = await client.post("/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    assert r.status_code == 401


async def test_refresh_reuse_revokes_all_sessions(client):
    await client.post("/auth/register", json=_USER)
    pair_a = (await client.post("/auth/login", json=_USER)).json()
    pair_b = (await client.post("/auth/login", json=_USER)).json()

    new_a = (
        await client.post("/auth/refresh", json={"refresh_token": pair_a["refresh_token"]})
    ).json()

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


async def test_logout_all_revokes_every_session(client):
    await client.post("/auth/register", json=_USER)
    pair_a = (await client.post("/auth/login", json=_USER)).json()
    pair_b = (await client.post("/auth/login", json=_USER)).json()

    r = await client.post(
        "/auth/logout-all", headers={"Authorization": f"Bearer {pair_a['access_token']}"}
    )
    assert r.status_code == 204

    for pair in (pair_a, pair_b):
        r = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert r.status_code == 401
