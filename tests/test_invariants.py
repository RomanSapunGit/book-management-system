from __future__ import annotations

import io

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


async def _genre(client, name: str) -> str:
    r = await client.get("/genres")
    return next(g["id"] for g in r.json()["items"] if g["name"] == name)


async def _author(client, headers, name: str) -> str:
    r = await client.post("/authors", json={"name": name}, headers=headers)
    return r.json()["id"]


async def _book(client, headers, *, title, genre_id, author_ids, published_year=2000):
    r = await client.post(
        "/books",
        json={
            "title": title,
            "genre_id": genre_id,
            "author_ids": author_ids,
            "published_year": published_year,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _csv(text: str):
    return ("books.csv", io.BytesIO(text.encode()), "text/csv")


async def test_year_validation_rejects_out_of_bounds(client, auth_headers):
    aid = await _author(client, auth_headers, "Y")
    fantasy = await _genre(client, "Fantasy")
    r = await client.post(
        "/books",
        json={"title": "Too old", "published_year": 1799, "author_ids": [aid], "genre_id": fantasy},
        headers=auth_headers,
    )
    assert r.status_code == 422
    r = await client.post(
        "/books",
        json={"title": "Too new", "published_year": 9999, "author_ids": [aid], "genre_id": fantasy},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_patch_empty_author_list_is_409(client, auth_headers):
    fantasy = await _genre(client, "Fantasy")
    aid = await _author(client, auth_headers, "Z")
    book = await _book(client, auth_headers, title="X", genre_id=fantasy, author_ids=[aid])
    r = await client.patch(f"/books/{book['id']}", json={"author_ids": []}, headers=auth_headers)
    assert r.status_code == 409


async def test_delete_genre_referenced_by_book_is_409(client, auth_headers):
    fantasy = await _genre(client, "Fantasy")
    aid = await _author(client, auth_headers, "Some")
    await _book(client, auth_headers, title="X", genre_id=fantasy, author_ids=[aid])
    r = await client.delete(f"/genres/{fantasy}", headers=auth_headers)
    assert r.status_code == 409


async def test_similar_books_scores_authors_above_genre(client, auth_headers):
    fantasy = await _genre(client, "Fantasy")
    mystery = await _genre(client, "Mystery")
    a_shared = await _author(client, auth_headers, "Shared")
    a_other = await _author(client, auth_headers, "Other")

    src = await _book(client, auth_headers, title="Source", genre_id=fantasy, author_ids=[a_shared])
    await _book(client, auth_headers, title="SameAuthor", genre_id=mystery, author_ids=[a_shared])
    await _book(client, auth_headers, title="SameGenre", genre_id=fantasy, author_ids=[a_other])

    r = await client.get(f"/books/{src['id']}/similar")
    items = r.json()["items"]
    titles = [b["title"] for b in items]
    assert titles.index("SameAuthor") < titles.index("SameGenre")
    by_title = {b["title"]: b for b in items}
    assert by_title["SameAuthor"]["score"] == 4
    assert by_title["SameGenre"]["score"] == 3


async def test_import_partial_failure_preserves_good_rows(client, auth_headers):
    csv = (
        "title,genre,authors,published_year\n"
        "Good,Fantasy,Alice,2000\n"
        "Bad,NotARealGenre,Bob,2001\n"
        "AlsoGood,Mystery,Carol,2002\n"
    )
    r = await client.post("/books/import", files={"file": _csv(csv)}, headers=auth_headers)
    body = r.json()
    assert body["successful"] == 2
    assert body["failed"] == 1
    r = await client.get("/books")
    titles = {b["title"] for b in r.json()["items"]}
    assert titles == {"Good", "AlsoGood"}


async def test_import_size_limit_returns_413(client, auth_headers):
    huge = "title,authors\n" + ("x,a\n" * 800_000)
    r = await client.post("/books/import", files={"file": _csv(huge)}, headers=auth_headers)
    assert r.status_code == 413


async def test_import_rate_limit_returns_429(client, auth_headers):
    csv = "title,genre,authors\nX,Fiction,A\n"
    for _ in range(5):
        r = await client.post("/books/import", files={"file": _csv(csv)}, headers=auth_headers)
        assert r.status_code == 200, r.text
    r = await client.post("/books/import", files={"file": _csv(csv)}, headers=auth_headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
