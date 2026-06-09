"""Tests for api_server.split_message_text (long-message auto-splitting)."""
import pytest

from wendy.api_server import MAX_SPLIT_CHUNKS, split_message_text


def test_short_message_untouched():
    assert split_message_text("hello") == ["hello"]


def test_exactly_at_limit_untouched():
    text = "a" * 2000
    assert split_message_text(text) == [text]


def test_long_message_splits_under_limit():
    text = "word " * 1000  # 5000 chars
    chunks = split_message_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)


def test_no_content_lost_on_plain_text():
    paragraphs = [f"paragraph {i} " + "x" * 300 for i in range(12)]
    text = "\n\n".join(paragraphs)
    chunks = split_message_text(text)
    rejoined = " ".join(chunks).split()
    assert rejoined == text.split()


def test_prefers_paragraph_boundaries():
    text = ("a" * 1500) + "\n\n" + ("b" * 1500)
    chunks = split_message_text(text)
    assert chunks[0].rstrip() == "a" * 1500
    assert chunks[1].lstrip() == "b" * 1500


def test_code_fence_closed_and_reopened():
    code = "\n".join(f"line_{i} = {i}" for i in range(200))
    text = f"check this out:\n```python\n{code}\n```\ndone"
    chunks = split_message_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        # Every chunk must contain an even number of fence markers so it
        # renders as a complete code block on its own.
        assert chunk.count("```") % 2 == 0
    # Continuation chunks reopen with the language tag.
    assert any(c.startswith("```python\n") for c in chunks[1:])


def test_hard_cut_when_no_boundaries():
    text = "a" * 5000  # no spaces or newlines at all
    chunks = split_message_text(text)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


def test_unsplittable_monster_exceeds_chunk_cap():
    text = "a" * (2000 * (MAX_SPLIT_CHUNKS + 2))
    chunks = split_message_text(text)
    assert len(chunks) > MAX_SPLIT_CHUNKS  # handler converts this into an error


@pytest.mark.parametrize("limit", [100, 500, 2000])
def test_respects_custom_limit(limit):
    text = ("lorem ipsum dolor sit amet " * 200).strip()
    chunks = split_message_text(text, limit=limit)
    assert all(len(c) <= limit for c in chunks)
