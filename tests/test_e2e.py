from __future__ import annotations

import io

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


async def _genre(client, name: str) -> str:
    r = await client.get("/genres")
    return next(g["id"] for g in r.json()["items"] if g["name"] == name)


async def test_curator_full_lifecycle(client, auth_headers):
    fantasy = await _genre(client, "Fantasy")
    mystery = await _genre(client, "Mystery")

    tolkien = (
        await client.post("/authors", json={"name": "J.R.R. Tolkien"}, headers=auth_headers)
    ).json()
    pratchett = (
        await client.post("/authors", json={"name": "Terry Pratchett"}, headers=auth_headers)
    ).json()
    le_guin = (
        await client.post("/authors", json={"name": "Ursula K. Le Guin"}, headers=auth_headers)
    ).json()

    hobbit = (
        await client.post(
            "/books",
            json={
                "title": "The Hobbit",
                "author_ids": [tolkien["id"]],
                "genre_id": fantasy,
                "published_year": 1937,
            },
            headers=auth_headers,
        )
    ).json()
    discworld = (
        await client.post(
            "/books",
            json={
                "title": "Guards! Guards!",
                "author_ids": [pratchett["id"]],
                "genre_id": fantasy,
                "published_year": 1989,
            },
            headers=auth_headers,
        )
    ).json()
    await client.post(
        "/books",
        json={
            "title": "A Wizard of Earthsea",
            "author_ids": [le_guin["id"]],
            "genre_id": fantasy,
            "published_year": 1968,
        },
        headers=auth_headers,
    )

    r = await client.get("/books", params={"genre": "Fantasy", "sort_by": "published_year", "sort_dir": "asc"})
    body = r.json()
    assert body["total"] == 3
    assert [b["title"] for b in body["items"]] == ["The Hobbit", "A Wizard of Earthsea", "Guards! Guards!"]

    r = await client.patch(f"/books/{hobbit['id']}", json={"genre_id": mystery}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["genre"]["name"] == "Mystery"

    r = await client.get(f"/books/{discworld['id']}/similar")
    similar_titles = [b["title"] for b in r.json()["items"]]
    assert "A Wizard of Earthsea" in similar_titles
    assert "The Hobbit" not in similar_titles

    r = await client.delete(f"/books/{hobbit['id']}", headers=auth_headers)
    assert r.status_code == 204
    r = await client.get(f"/books/{hobbit['id']}")
    assert r.status_code == 404


async def test_bulk_import_journey(client, auth_headers):
    csv = (
        "title,genre,authors,published_year\n"
        "Dune,Science Fiction,Frank Herbert,1965\n"
        ",Fiction,Anon,2000\n"
        "Foundation,Science Fiction,Isaac Asimov,1951\n"
        "Bad,NotARealGenre,Carl,2001\n"
    )
    r = await client.post(
        "/books/import",
        files={"file": ("books.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["received"] == 4
    assert body["successful"] == 2
    assert body["failed"] == 2
    assert {e["row"] for e in body["errors"]} == {2, 4}

    r = await client.get("/books")
    titles = {b["title"] for b in r.json()["items"]}
    assert {"Dune", "Foundation"} <= titles

    r = await client.get("/books/import", headers=auth_headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["successful"] == 2

    await client.post("/auth/register", json={"email": "bob@example.com", "password": "very-strong-password"})
    pair = (
        await client.post("/auth/login", json={"email": "bob@example.com", "password": "very-strong-password"})
    ).json()
    other_headers = {"Authorization": f"Bearer {pair['access_token']}"}
    r = await client.get("/books/import", headers=other_headers)
    assert r.json()["total"] == 0
