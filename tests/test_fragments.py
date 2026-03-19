"""Tests for wendy.fragments."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from wendy.fragments import (
    Fragment,
    execute_select,
    get_new_context_introductions,
    load_fragments,
    matches_context,
    parse_fragment,
    parse_frontmatter,
    reset_introductions,
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


def test_parse_fragment_sticky(tmp_path):
    f = tmp_path / "topic_sticky.md"
    f.write_text("---\ntype: topic\norder: 1\nkeywords: [test]\nsticky: 3\n---\nContent.")
    frag = parse_fragment(f)
    assert frag is not None
    assert frag.sticky == 3


def test_parse_fragment_sticky_default(tmp_path):
    f = tmp_path / "topic_no_sticky.md"
    f.write_text("---\ntype: topic\norder: 1\nkeywords: [test]\n---\nContent.")
    frag = parse_fragment(f)
    assert frag is not None
    assert frag.sticky is None


def test_parse_fragment_description(tmp_path):
    f = tmp_path / "people_alice.md"
    f.write_text("---\ntype: person\norder: 50\nkeywords: [alice]\ndescription: a helpful server regular\n---\nAlice info.")
    frag = parse_fragment(f)
    assert frag is not None
    assert frag.description == "a helpful server regular"


def test_parse_fragment_behavioral(tmp_path):
    f = tmp_path / "topic_style.md"
    f.write_text("---\ntype: topic\norder: 10\nkeywords: [magic]\nbehavioral: true\n---\nBehavioral topic.")
    frag = parse_fragment(f)
    assert frag is not None
    assert frag.behavioral is True


def test_parse_fragment_behavioral_default(tmp_path):
    f = tmp_path / "topic_plain.md"
    f.write_text("---\ntype: topic\norder: 10\nkeywords: [something]\n---\nPlain topic.")
    frag = parse_fragment(f)
    assert frag is not None
    assert frag.behavioral is False


def test_people_dir_with_frontmatter(tmp_path):
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "alice.md").write_text(
        "---\ntype: person\norder: 50\nkeywords: [alice]\nmatch_authors: true\n---\nAlice content."
    )
    frags = scan_fragments(tmp_path)
    assert len(frags) == 1
    assert frags[0].type == "person"
    assert frags[0].content == "Alice content."
    assert frags[0].keywords == ["alice"]


def test_people_dir_no_frontmatter(tmp_path):
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "bob-smith.md").write_text("Bob is a cool guy.")
    frags = scan_fragments(tmp_path)
    assert len(frags) == 1
    frag = frags[0]
    assert frag.type == "person"
    assert frag.match_authors is True
    assert "bob-smith" in frag.keywords
    assert "bob" in frag.keywords
    assert "smith" in frag.keywords
    assert frag.content == "Bob is a cool guy."


def test_scan_fragments_includes_people_dir(tmp_path):
    (tmp_path / "common_01.md").write_text("---\ntype: common\norder: 1\n---\nCommon")
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "charlie.md").write_text("Charlie is around.")
    frags = scan_fragments(tmp_path)
    types = {f.type for f in frags}
    assert "common" in types
    assert "person" in types
    assert len(frags) == 2


def test_topic_sticky_per_fragment(tmp_path):
    """Per-fragment sticky overrides TOPIC_STICKY_TURNS (behavioral topics only)."""
    (tmp_path / "topic_short.md").write_text(
        "---\ntype: topic\norder: 1\nkeywords: [rareword]\nsticky: 1\nbehavioral: true\n---\nShort sticky."
    )
    # First call: keyword matches, topic loaded and state recorded
    result1 = load_fragments("123", "test", messages=[{"content": "rareword here"}],
                             authors=[], frag_dir=tmp_path, state_dir=tmp_path)
    assert "Short sticky." in result1["topics"]

    # Second call: keyword gone, sticky=1 so still loaded (turns_stale=1 <= 1)
    result2 = load_fragments("123", "test", messages=[{"content": "something else"}],
                             authors=[], frag_dir=tmp_path, state_dir=tmp_path)
    assert "Short sticky." in result2["topics"]

    # Third call: turns_stale=2 > sticky=1, should drop
    result3 = load_fragments("123", "test", messages=[{"content": "something else"}],
                             authors=[], frag_dir=tmp_path, state_dir=tmp_path)
    assert "Short sticky." not in result3["topics"]


def test_load_fragments_returns_sections(tmp_path):
    (tmp_path / "common_01.md").write_text("---\ntype: common\norder: 1\n---\nCommon stuff")
    (tmp_path / "anchor_01.md").write_text("---\ntype: anchor\norder: 1\n---\nAnchor stuff")

    result = load_fragments("123", "test", messages=[], authors=[], frag_dir=tmp_path, state_dir=tmp_path)
    assert "channel" in result
    assert "persons" in result
    assert "topics" in result
    assert "anchors" in result
    assert "Common stuff" in result["channel"]
    assert "Anchor stuff" in result["anchors"]
    # persons is always empty string now -- injected via synthetic messages
    assert result["persons"] == ""


def test_load_fragments_skips_non_behavioral_topics(tmp_path):
    """Non-behavioral topics should not appear in the topics section."""
    (tmp_path / "topic_plain.md").write_text(
        "---\ntype: topic\norder: 1\nkeywords: [magic]\n---\nPlain topic content."
    )
    result = load_fragments("123", "test", messages=[{"content": "magic here"}],
                            authors=[], frag_dir=tmp_path, state_dir=tmp_path)
    assert "Plain topic content." not in result["topics"]


def test_load_fragments_includes_behavioral_topics(tmp_path):
    """behavioral: true topics should appear in the topics section when matched."""
    (tmp_path / "topic_style.md").write_text(
        "---\ntype: topic\norder: 1\nkeywords: [magic]\nbehavioral: true\n---\nBehavioral topic content."
    )
    result = load_fragments("123", "test", messages=[{"content": "magic here"}],
                            authors=[], frag_dir=tmp_path, state_dir=tmp_path)
    assert "Behavioral topic content." in result["topics"]


def test_get_new_context_introductions_person(tmp_path):
    """First mention of a person yields an intro; second mention does not."""
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "alice.md").write_text(
        "---\ntype: person\norder: 50\nkeywords: [alice]\nmatch_authors: true\n"
        "description: a friendly server regular\n---\nAlice info."
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    msgs = [{"author": "alice", "author_id": 0, "content": "hello"}]

    # First call: alice not yet introduced
    intros = get_new_context_introductions(
        "test", "session-abc", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert len(intros) == 1
    assert "alice" in intros[0]
    assert "a friendly server regular" in intros[0]
    assert "Full profile:" in intros[0]

    # Second call: alice already introduced -- no repeat
    intros2 = get_new_context_introductions(
        "test", "session-abc", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert intros2 == []


def test_get_new_context_introductions_session_reset(tmp_path):
    """Session change clears introduced list -- person gets re-introduced."""
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "bob.md").write_text("Bob is around.")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    msgs = [{"author": "bob", "author_id": 0, "content": "hi"}]

    intros1 = get_new_context_introductions(
        "test", "session-1", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert len(intros1) == 1

    # Same session: no re-introduction
    intros2 = get_new_context_introductions(
        "test", "session-1", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert intros2 == []

    # New session: re-introduction fires
    intros3 = get_new_context_introductions(
        "test", "session-2", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert len(intros3) == 1


def test_get_new_context_introductions_skips_behavioral(tmp_path):
    """behavioral: true topic fragments are not injected via this function."""
    (tmp_path / "topic_style.md").write_text(
        "---\ntype: topic\norder: 10\nkeywords: [magic]\nbehavioral: true\n---\nBehavioral content."
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    msgs = [{"author": "user", "author_id": 0, "content": "magic is here"}]
    intros = get_new_context_introductions(
        "test", "session-abc", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert intros == []


def test_get_new_context_introductions_non_behavioral_topic(tmp_path):
    """Non-behavioral topic fragments are injected when keywords match."""
    (tmp_path / "topic_info.md").write_text(
        "---\ntype: topic\norder: 10\nkeywords: [pokemon]\n"
        "description: relates to the GBA project\n---\nPokemon info."
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    msgs = [{"author": "user", "author_id": 0, "content": "let's talk about pokemon"}]
    intros = get_new_context_introductions(
        "test", "session-abc", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert len(intros) == 1
    assert "pokemon" in intros[0].lower()
    assert "relates to the GBA project" in intros[0]
    assert "Reference:" in intros[0]


def test_reset_introductions(tmp_path):
    """reset_introductions clears introduced keys but keeps session_id."""
    people_dir = tmp_path / "people"
    people_dir.mkdir()
    (people_dir / "carol.md").write_text("Carol info.")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    msgs = [{"author": "carol", "author_id": 0, "content": "hi"}]

    # Introduce carol
    get_new_context_introductions(
        "test", "session-xyz", msgs, frag_dir=tmp_path, state_dir=state_dir
    )

    # Verify carol is introduced (second call yields nothing)
    intros = get_new_context_introductions(
        "test", "session-xyz", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert intros == []

    # Reset (simulating compaction)
    reset_introductions("test", state_dir=state_dir)

    # After reset: same session, carol should be re-introduced
    intros_after = get_new_context_introductions(
        "test", "session-xyz", msgs, frag_dir=tmp_path, state_dir=state_dir
    )
    assert len(intros_after) == 1
