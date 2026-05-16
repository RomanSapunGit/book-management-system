from __future__ import annotations

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


async def _seeded_genre_id(client, name: str = "Fantasy") -> str:
    r = await client.get("/genres")
    for g in r.json()["items"]:
        if g["name"] == name:
            return g["id"]
    raise AssertionError(f"genre {name} not seeded")


async def test_create_requires_auth(client):
    r = await client.post("/genres", json={"name": "Cyberpunk"})
    assert r.status_code == 401


async def test_create_derives_slug(client, auth_headers):
    r = await client.post("/genres", json={"name": "Cyberpunk"}, headers=auth_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Cyberpunk"
    assert body["slug"] == "cyberpunk"

    r2 = await client.post("/genres", json={"name": "Science Fantasy"}, headers=auth_headers)
    assert r2.json()["slug"] == "science-fantasy"


async def test_create_case_insensitive_duplicate_is_409(client, auth_headers):
    r1 = await client.post("/genres", json={"name": "Cyberpunk"}, headers=auth_headers)
    assert r1.status_code == 201
    r2 = await client.post("/genres", json={"name": "CYBERPUNK"}, headers=auth_headers)
    assert r2.status_code == 409


async def test_create_rejects_empty_name(client, auth_headers):
    r = await client.post("/genres", json={"name": "   "}, headers=auth_headers)
    assert r.status_code == 422


async def test_patch_renames_and_reslugs(client, auth_headers):
    r = await client.post("/genres", json={"name": "Cyberpunk"}, headers=auth_headers)
    gid = r.json()["id"]
    r = await client.patch(f"/genres/{gid}", json={"name": "Post-Cyberpunk"}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Post-Cyberpunk"
    assert body["slug"] == "post-cyberpunk"


async def test_patch_empty_is_422(client, auth_headers):
    gid = await _seeded_genre_id(client)
    r = await client.patch(f"/genres/{gid}", json={}, headers=auth_headers)
    assert r.status_code == 422


async def test_patch_unknown_field_is_422(client, auth_headers):
    gid = await _seeded_genre_id(client)
    r = await client.patch(f"/genres/{gid}", json={"slug": "manual-slug"}, headers=auth_headers)
    assert r.status_code == 422


async def test_delete_unreferenced_genre(client, auth_headers):
    r = await client.post("/genres", json={"name": "Tossable"}, headers=auth_headers)
    gid = r.json()["id"]
    r = await client.delete(f"/genres/{gid}", headers=auth_headers)
    assert r.status_code == 204
    r = await client.get(f"/genres/{gid}")
    assert r.status_code == 404


async def test_delete_referenced_genre_is_409(client, auth_headers):
    fantasy = await _seeded_genre_id(client, "Fantasy")
    r = await client.post("/authors", json={"name": "Some Author"}, headers=auth_headers)
    aid = r.json()["id"]
    r = await client.post(
        "/books",
        json={"title": "X", "genre_id": fantasy, "author_ids": [aid], "published_year": 2000},
        headers=auth_headers,
    )
    assert r.status_code == 201

    r = await client.delete(f"/genres/{fantasy}", headers=auth_headers)
    assert r.status_code == 409


async def test_get_genre_404(client):
    r = await client.get("/genres/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
