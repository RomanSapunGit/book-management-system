from __future__ import annotations

import io
import json

import pytest

from tests.conftest import pg_available

pytestmark = pytest.mark.skipif(not pg_available, reason="Postgres not reachable")


def _json_file(payload):
    return ("books.json", io.BytesIO(json.dumps(payload).encode()), "application/json")


def _csv_file(text: str):
    return ("books.csv", io.BytesIO(text.encode()), "text/csv")


async def test_import_requires_auth(client):
    r = await client.post("/books/import", files={"file": _csv_file("title\nX\n")})
    assert r.status_code == 401


async def test_import_json_partial_failure(client, auth_headers):
    payload = {
        "books": [
            {"title": "Good 1", "genre": "Fantasy", "authors": ["A; B"], "published_year": 2000},
            {"title": "", "genre": "Fiction", "authors": ["x"]},
            {"title": "Good 2", "genre": "Fiction", "authors": ["C"], "published_year": 2024},
            {"title": "Bad year", "genre": "Fiction", "authors": ["D"], "published_year": 1500},
        ]
    }
    r = await client.post(
        "/books/import", files={"file": _json_file(payload)}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["received"] == 4
    assert body["successful"] == 2
    assert body["failed"] == 2
    rows_with_errors = {e["row"] for e in body["errors"]}
    assert rows_with_errors == {2, 4}
    for e in body["errors"]:
        assert e.get("reason")

    r = await client.get("/books")
    titles = {b["title"] for b in r.json()["items"]}
    assert titles == {"Good 1", "Good 2"}


async def test_import_csv_happy_path(client, auth_headers):
    csv = "title,genre,authors,published_year\nFoo,Fantasy,Alice;Bob,1999\nBar,Mystery,Carol,2010\n"
    r = await client.post("/books/import", files={"file": _csv_file(csv)}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["successful"] == 2
    assert body["failed"] == 0
    assert body["errors"] == []


async def test_import_unknown_genre_rolls_back_only_that_row(client, auth_headers):
    csv = (
        "title,genre,authors,published_year\n"
        "Good,Fantasy,Alice,2000\n"
        "Bad,NotARealGenre,Bob,2001\n"
        "AlsoGood,Mystery,Carol,2002\n"
    )
    r = await client.post("/books/import", files={"file": _csv_file(csv)}, headers=auth_headers)
    body = r.json()
    assert body["successful"] == 2
    assert body["failed"] == 1
    assert any(e["row"] == 2 and "unknown genre" in e["reason"].lower() for e in body["errors"])

    r = await client.get("/books")
    titles = {b["title"] for b in r.json()["items"]}
    assert titles == {"Good", "AlsoGood"}


async def test_import_size_limit(client, auth_headers):
    huge = "title,authors\n" + ("x,a\n" * 800_000)
    r = await client.post("/books/import", files={"file": _csv_file(huge)}, headers=auth_headers)
    assert r.status_code == 413


async def test_import_wrong_content_type(client, auth_headers):
    r = await client.post(
        "/books/import",
        files={"file": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 415


async def test_import_invalid_json_is_a_session_failure(client, auth_headers):
    r = await client.post(
        "/books/import",
        files={"file": ("x.json", io.BytesIO(b"{not valid json"), "application/json")},
        headers=auth_headers,
    )
    assert r.status_code == 400


async def test_import_session_appears_in_list(client, auth_headers):
    r = await client.post(
        "/books/import",
        files={
            "file": _json_file({"books": [{"title": "T", "genre": "Fiction", "authors": ["A"]}]})
        },
        headers=auth_headers,
    )
    session_id = r.json()["id"]

    r2 = await client.get("/books/import", headers=auth_headers)
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == session_id
    assert body["items"][0]["successful"] == 1


async def test_import_session_isolation_between_users(client, auth_headers):
    await client.post(
        "/books/import",
        files={
            "file": _json_file({"books": [{"title": "T", "genre": "Fiction", "authors": ["A"]}]})
        },
        headers=auth_headers,
    )

    await client.post(
        "/auth/register", json={"email": "other@example.com", "password": "another-strong-pass"}
    )
    pair = (
        await client.post(
            "/auth/login", json={"email": "other@example.com", "password": "another-strong-pass"}
        )
    ).json()
    other_headers = {"Authorization": f"Bearer {pair['access_token']}"}

    r = await client.get("/books/import", headers=other_headers)
    assert r.status_code == 200
    assert r.json()["total"] == 0


async def test_import_rate_limit(client, auth_headers):
    csv = "title,genre,authors\nX,Fiction,A\n"
    for _ in range(5):
        r = await client.post("/books/import", files={"file": _csv_file(csv)}, headers=auth_headers)
        assert r.status_code == 200, r.text

    r = await client.post("/books/import", files={"file": _csv_file(csv)}, headers=auth_headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
