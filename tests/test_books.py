from __future__ import annotations

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


async def _genre_id(client, name: str = "Fantasy") -> str:
    r = await client.get("/genres")
    for g in r.json()["items"]:
        if g["name"] == name:
            return g["id"]
    raise AssertionError(f"genre {name} not seeded")


async def _author(client, headers, name: str) -> str:
    r = await client.post("/authors", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _book(client, headers, **overrides):
    if "author_ids" not in overrides:
        overrides["author_ids"] = [await _author(client, headers, "J. R. R. Tolkien")]
    if "genre_id" not in overrides:
        overrides["genre_id"] = await _genre_id(client)
    payload = {"title": "The Hobbit", "published_year": 1937} | overrides
    r = await client.post("/books", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_mutating_endpoints_require_auth(client):
    r = await client.post("/books", json={"title": "X", "author_ids": []})
    assert r.status_code == 401
    r = await client.patch("/books/00000000-0000-0000-0000-000000000000", json={"title": "Y"})
    assert r.status_code == 401
    r = await client.delete("/books/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 401
    r = await client.post("/authors", json={"name": "Anon"})
    assert r.status_code == 401


async def test_reads_are_public(client):
    r = await client.get("/books")
    assert r.status_code == 200
    r = await client.get("/authors")
    assert r.status_code == 200
    r = await client.get("/genres")
    assert r.status_code == 200


async def test_create_returns_nested_author_and_genre(client, auth_headers):
    book = await _book(client, auth_headers)
    assert book["genre"]["name"] == "Fantasy"
    assert book["authors"][0]["name"] == "J. R. R. Tolkien"


async def test_create_rejects_book_without_author(client, auth_headers):
    r = await client.post("/books", json={"title": "Orphan", "author_ids": []}, headers=auth_headers)
    assert r.status_code == 422


async def test_create_rejects_unknown_author_id(client, auth_headers):
    r = await client.post(
        "/books",
        json={"title": "X", "author_ids": ["00000000-0000-0000-0000-000000000000"]},
        headers=auth_headers,
    )
    assert r.status_code == 404


async def test_create_rejects_duplicate_author_ids_in_request(client, auth_headers):
    aid = await _author(client, auth_headers, "Dup")
    r = await client.post(
        "/books", json={"title": "X", "author_ids": [aid, aid]}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_year_validation_bounds(client, auth_headers):
    aid = await _author(client, auth_headers, "Y")
    r = await client.post(
        "/books", json={"title": "Too old", "published_year": 1799, "author_ids": [aid]}, headers=auth_headers
    )
    assert r.status_code == 422

    r = await client.post(
        "/books", json={"title": "Too new", "published_year": 9999, "author_ids": [aid]}, headers=auth_headers
    )
    assert r.status_code == 422


async def test_list_filters_and_sorts(client, auth_headers):
    fantasy = await _genre_id(client, "Fantasy")
    scifi = await _genre_id(client, "Science Fiction")
    a1 = await _author(client, auth_headers, "X")
    a2 = await _author(client, auth_headers, "Y")
    await _book(client, auth_headers, title="Aaa", published_year=2001, genre_id=scifi, author_ids=[a1])
    await _book(client, auth_headers, title="Bbb", published_year=1999, genre_id=fantasy, author_ids=[a2])
    await _book(client, auth_headers, title="Ccc", published_year=2010, genre_id=scifi, author_ids=[a1])

    r = await client.get("/books", params={"genre": "science fiction", "sort_by": "published_year", "sort_dir": "asc"})
    body = r.json()
    assert body["total"] == 2
    assert [b["title"] for b in body["items"]] == ["Aaa", "Ccc"]

    r = await client.get("/books", params={"author": "y"})
    assert r.json()["total"] == 1

    r = await client.get("/books", params={"year_from": 2000, "year_to": 2005})
    assert [b["title"] for b in r.json()["items"]] == ["Aaa"]


async def test_year_range_validation(client):
    r = await client.get("/books", params={"year_from": 2020, "year_to": 2000})
    assert r.status_code == 422


async def test_patch_partial(client, auth_headers):
    book = await _book(client, auth_headers)
    new_genre = await _genre_id(client, "Mystery")
    r = await client.patch(f"/books/{book['id']}", json={"genre_id": new_genre}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["genre"]["name"] == "Mystery"
    assert r.json()["title"] == book["title"]


async def test_patch_clears_nullable(client, auth_headers):
    book = await _book(client, auth_headers)
    r = await client.patch(f"/books/{book['id']}", json={"description": None}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["description"] is None


async def test_patch_empty_is_422(client, auth_headers):
    book = await _book(client, auth_headers)
    r = await client.patch(f"/books/{book['id']}", json={}, headers=auth_headers)
    assert r.status_code == 422


async def test_patch_rejects_unknown_field(client, auth_headers):
    book = await _book(client, auth_headers)
    r = await client.patch(f"/books/{book['id']}", json={"isbn": "x"}, headers=auth_headers)
    assert r.status_code == 422


async def test_patch_empty_author_list_rejected(client, auth_headers):
    book = await _book(client, auth_headers)
    r = await client.patch(f"/books/{book['id']}", json={"author_ids": []}, headers=auth_headers)
    assert r.status_code == 409


async def test_delete(client, auth_headers):
    book = await _book(client, auth_headers)
    r = await client.delete(f"/books/{book['id']}", headers=auth_headers)
    assert r.status_code == 204
    r = await client.get(f"/books/{book['id']}")
    assert r.status_code == 404


async def test_author_create_allows_duplicate_names(client, auth_headers):
    r1 = await client.post("/authors", json={"name": "Pratchett"}, headers=auth_headers)
    assert r1.status_code == 201
    r2 = await client.post("/authors", json={"name": "PRATCHETT"}, headers=auth_headers)
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


async def test_list_genres_returns_seeded_set(client):
    r = await client.get("/genres")
    assert r.status_code == 200
    names = {g["name"] for g in r.json()["items"]}
    assert {"Fiction", "Fantasy", "Science Fiction"} <= names


async def test_request_id_echoed(client):
    r = await client.get("/books", headers={"X-Request-ID": "test-rid-123"})
    assert r.headers.get("X-Request-ID") == "test-rid-123"


async def test_health_includes_db(client):
    r = await client.get("/health")
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


async def test_similar_books_scores_authors_above_genre(client, auth_headers):
    fantasy = await _genre_id(client, "Fantasy")
    a_shared = await _author(client, auth_headers, "Shared Author")
    a_other = await _author(client, auth_headers, "Other Author")

    src = await _book(client, auth_headers, title="Source", genre_id=fantasy, author_ids=[a_shared], published_year=2000)
    same_author = await _book(
        client, auth_headers, title="SameAuthor",
        genre_id=await _genre_id(client, "Mystery"), author_ids=[a_shared], published_year=2000,
    )
    same_genre = await _book(
        client, auth_headers, title="SameGenre",
        genre_id=fantasy, author_ids=[a_other], published_year=2000,
    )

    r = await client.get(f"/books/{src['id']}/similar")
    assert r.status_code == 200
    items = r.json()["items"]
    titles_in_order = [b["title"] for b in items]
    assert titles_in_order.index("SameAuthor") < titles_in_order.index("SameGenre")

    by_title = {b["title"]: b for b in items}
    assert by_title["SameAuthor"]["score"] == 4
    assert by_title["SameGenre"]["score"] == 3


async def test_similar_books_excludes_source(client, auth_headers):
    book = await _book(client, auth_headers)
    r = await client.get(f"/books/{book['id']}/similar")
    assert book["id"] not in {b["id"] for b in r.json()["items"]}


async def test_similar_books_empty_when_no_matches(client, auth_headers):
    a1 = await _author(client, auth_headers, "Alone")
    a2 = await _author(client, auth_headers, "Different")
    src = await _book(
        client, auth_headers, title="Solo",
        genre_id=await _genre_id(client, "Fantasy"), author_ids=[a1], published_year=2000,
    )
    await _book(
        client, auth_headers, title="Unrelated",
        genre_id=await _genre_id(client, "Mystery"), author_ids=[a2], published_year=1850,
    )

    r = await client.get(f"/books/{src['id']}/similar")
    assert r.json()["total"] == 0
    assert r.json()["items"] == []


async def test_similar_books_paginates(client, auth_headers):
    fantasy = await _genre_id(client, "Fantasy")
    src_author = await _author(client, auth_headers, "Src Author")
    src = await _book(
        client, auth_headers, title="Source", genre_id=fantasy,
        author_ids=[src_author], published_year=2000,
    )
    for i in range(5):
        other = await _author(client, auth_headers, f"Author {i}")
        await _book(
            client, auth_headers, title=f"Candidate {i}", genre_id=fantasy,
            author_ids=[other], published_year=2000,
        )

    r = await client.get(f"/books/{src['id']}/similar", params={"limit": 2, "offset": 0})
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    first_page_ids = {b["id"] for b in body["items"]}

    r = await client.get(f"/books/{src['id']}/similar", params={"limit": 2, "offset": 2})
    second_page_ids = {b["id"] for b in r.json()["items"]}
    assert first_page_ids.isdisjoint(second_page_ids)


async def test_similar_books_404_on_missing(client):
    r = await client.get("/books/00000000-0000-0000-0000-000000000000/similar")
    assert r.status_code == 404


async def test_error_body_includes_request_id(client):
    r = await client.get("/books/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    body = r.json()
    assert "request_id" in body
    assert body["request_id"] == r.headers["X-Request-ID"]
