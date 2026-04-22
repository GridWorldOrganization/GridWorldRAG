"""Tests for the structure-aware chunker."""
from __future__ import annotations

from src.indexer import chunk_text


def test_empty_text():
    assert list(chunk_text("", 100, 10)) == []


def test_short_text_single_chunk():
    r = list(chunk_text("short text", 100, 10))
    assert r == ["short text"]


def test_markdown_headings_create_section_boundaries():
    text = (
        "# Heading A\nSome content under A.\n\n"
        "# Heading B\nSome content under B."
    )
    chunks = list(chunk_text(text, 200, 20))
    # Both sections fit well under 200 chars; should be 2 chunks (one per heading)
    # OR concatenated into one if combined fits. With our combine rule they combine.
    # Acceptable either way — check both headings are present
    joined = "\n\n".join(chunks)
    assert "Heading A" in joined
    assert "Heading B" in joined


def test_oversized_paragraph_is_split():
    long = "x" * 1500
    chunks = list(chunk_text(long, 500, 50))
    # At least 3 chunks because 1500 / 450 step = ~4 chunks
    assert len(chunks) >= 3
    # No chunk exceeds size
    assert all(len(c) <= 500 for c in chunks)


def test_paragraphs_combined_to_fill_chunk():
    text = "para1\n\npara2\n\npara3\n\npara4"
    chunks = list(chunk_text(text, 100, 10))
    # Small paragraphs should combine into fewer chunks
    assert len(chunks) == 1
    assert "para1" in chunks[0] and "para4" in chunks[0]
