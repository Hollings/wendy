"""Tests for wendy.fragments."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from wendy.fragments import (
    Fragment,
    execute_select,
    load_fragments,
    matches_context,
    parse_fragment,
    parse_frontmatter,
    scan_fragments,
)


def test_parse_frontmatter_valid():
    text = dedent("""\
        ---
        type: topic
        order: 1
        keywords: [osrs, runescape]
        ---
        Some content here.
    """)
    meta, body = parse_frontmatter(text)
    assert meta is not None
    assert meta["type"] == "topic"
    assert meta["order"] == 1
    assert meta["keywords"] == ["osrs", "runescape"]
    assert body.strip() == "Some content here."


def test_parse_frontmatter_missing():
    text = "No frontmatter here."
    meta, body = parse_frontmatter(text)
    assert meta is None
    assert body == text


def test_parse_frontmatter_invalid_yaml():
    text = "---\n: invalid: yaml: here\n---\nContent"
    meta, body = parse_frontmatter(text)
    # Should return None meta on parse error
    assert meta is None


def test_parse_fragment_valid(tmp_path):
    f = tmp_path / "topic_01_test.md"
    f.write_text(dedent("""\
        ---
        type: topic
        order: 1
        keywords: [test]
        ---
        Test content.
    """))
    frag = parse_fragment(f)
    assert frag is not None
    assert frag.type == "topic"
    assert frag.order == 1
    assert frag.keywords == ["test"]
    assert frag.content == "Test content."


def test_parse_fragment_invalid_type(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("---\ntype: invalid_type\n---\nContent")
    frag = parse_fragment(f)
    assert frag is None


def test_parse_fragment_no_frontmatter(tmp_path):
    f = tmp_path / "plain.md"
    f.write_text("Just plain markdown, no frontmatter.")
    frag = parse_fragment(f)
    assert frag is None


def test_scan_fragments(tmp_path):
    (tmp_path / "common_01.md").write_text("---\ntype: common\norder: 1\n---\nCommon content")
    (tmp_path / "topic_01.md").write_text("---\ntype: topic\norder: 1\nkeywords: [test]\n---\nTopic content")
    (tmp_path / "not_a_fragment.txt").write_text("Not markdown")

    frags = scan_fragments(tmp_path)
    assert len(frags) == 2


def test_matches_context_common():
    frag = Fragment(path=Path("x.md"), type="common", order=1, channel="", keywords=[], match_authors=False, select="", content="c")
    assert matches_context(frag, [], [], "123") is True


def test_matches_context_anchor():
    frag = Fragment(path=Path("x.md"), type="anchor", order=1, channel="", keywords=[], match_authors=False, select="", content="c")
    assert matches_context(frag, [], [], "123") is True


def test_matches_context_channel_match():
    frag = Fragment(path=Path("x.md"), type="channel", order=1, channel="123", keywords=[], match_authors=False, select="", content="c")
    assert matches_context(frag, [], [], "123") is True
    assert matches_context(frag, [], [], "999") is False


def test_matches_context_topic_keyword():
    frag = Fragment(path=Path("x.md"), type="topic", order=1, channel="", keywords=["python"], match_authors=False, select="", content="c")
    msgs = [{"content": "I love Python programming"}]
    assert matches_context(frag, msgs, [], "123") is True

    msgs_no_match = [{"content": "hello world"}]
    assert matches_context(frag, msgs_no_match, [], "123") is False


def test_matches_context_topic_match_authors():
    frag = Fragment(path=Path("x.md"), type="topic", order=1, channel="", keywords=["hollings"], match_authors=True, select="", content="c")
    msgs = [{"content": "unrelated"}]
    authors = ["hollings"]
    assert matches_context(frag, msgs, authors, "123") is True


def test_matches_context_person_no_rules():
    frag = Fragment(path=Path("x.md"), type="person", order=1, channel="", keywords=[], match_authors=False, select="", content="c")
    assert matches_context(frag, [], [], "123") is True


def test_execute_select_basic():
    code = 'return "python" in combined'
    assert execute_select(code, [{"content": "python code"}], [], "123", "python code") is True
    assert execute_select(code, [{"content": "java code"}], [], "123", "java code") is False


def test_execute_select_error():
    code = 'raise ValueError("oops")'
    assert execute_select(code, [], [], "123", "") is False


def test_load_fragments_returns_sections(tmp_path):
    (tmp_path / "common_01.md").write_text("---\ntype: common\norder: 1\n---\nCommon stuff")
    (tmp_path / "anchor_01.md").write_text("---\ntype: anchor\norder: 1\n---\nAnchor stuff")

    result = load_fragments("123", "test", messages=[], authors=[], frag_dir=tmp_path)
    assert "channel" in result
    assert "persons" in result
    assert "topics" in result
    assert "anchors" in result
    assert "Common stuff" in result["channel"]
    assert "Anchor stuff" in result["anchors"]
