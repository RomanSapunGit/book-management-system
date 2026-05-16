from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.books.bulk import _row_to_payload, _split_names
from app.books.schemas import validate_year


def test_validate_year_accepts_bounds():
    assert validate_year(1800) == 1800
    current = datetime.now(UTC).year
    assert validate_year(current) == current


def test_validate_year_rejects_below_1800():
    with pytest.raises(ValueError):
        validate_year(1799)


def test_validate_year_rejects_future():
    future = datetime.now(UTC).year + 1
    with pytest.raises(ValueError):
        validate_year(future)


def test_validate_year_passes_none():
    assert validate_year(None) is None


def test_split_names_semicolon():
    assert _split_names("A; B ;C") == ["A", "B", "C"]


def test_split_names_pipe():
    assert _split_names("A|B|C") == ["A", "B", "C"]


def test_split_names_single():
    assert _split_names("Solo Author") == ["Solo Author"]


def test_split_names_empty():
    assert _split_names("") == []
    assert _split_names(None) == []


def test_split_names_handles_list_input():
    assert _split_names(["A", "B", ""]) == ["A", "B"]


def test_row_to_payload_csv_shape():
    out = _row_to_payload({"title": "X", "authors": "A;B", "published_year": "2000", "genre": "Fiction"})
    assert out["title"] == "X"
    assert out["authors"] == ["A", "B"]
    assert out["published_year"] == 2000
    assert out["genre"] == "Fiction"


def test_row_to_payload_passes_through_bad_year_for_pydantic_to_complain():
    out = _row_to_payload({"title": "X", "authors": "A", "published_year": "not-a-number"})
    assert out["published_year"] == "not-a-number"
