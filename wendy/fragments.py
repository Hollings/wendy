"""Fragment loading with YAML frontmatter.

Port of v1 fragment_loader.py -- nearly verbatim since it's already clean.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
import textwrap
from pathlib import Path

import yaml

from .config import WENDY_BOT_ID
from .paths import DB_PATH, FRAGMENTS_DIR, channel_dir

_LOG = logging.getLogger(__name__)

VALID_TYPES = {"common", "channel", "person", "topic", "anchor"}

_SAFE_BUILTINS = {
    "any": any, "all": all, "len": len, "str": str, "int": int,
    "bool": bool, "list": list, "set": set, "min": min, "max": max,
    "sorted": sorted, "enumerate": enumerate, "zip": zip, "range": range,
    "isinstance": isinstance, "True": True, "False": False, "None": None,
}

_MAX_SELECT_LEN = 2000


@dataclasses.dataclass
class Fragment:
    """A parsed fragment file with its frontmatter metadata."""
    path: Path
    type: str
    order: int
    channel: str
    keywords: list[str]
    match_authors: bool
    select: str
    content: str
    sticky: int | None = None
    user_ids: list[int] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split YAML frontmatter from content body."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, text

    yaml_str = m.group(1)
    body = text[m.end():]

    try:
        meta = yaml.safe_load(yaml_str)
        if not isinstance(meta, dict):
            return None, text
        return meta, body
    except yaml.YAMLError as e:
        _LOG.warning("Failed to parse YAML frontmatter: %s", e)
        return None, text


def parse_fragment(path: Path) -> Fragment | None:
    """Read a file, parse frontmatter, validate, return Fragment."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _LOG.warning("Failed to read fragment %s: %s", path, e)
        return None

    meta, body = parse_frontmatter(text)
    if meta is None:
        return None

    ftype = meta.get("type")
    if ftype not in VALID_TYPES:
        _LOG.warning("Fragment %s has invalid type %r, skipping", path.name, ftype)
        return None

    order = int(meta.get("order", 50))
    channel = str(meta.get("channel", ""))
    keywords = meta.get("keywords", [])
    if not isinstance(keywords, list):
        keywords = [str(keywords)]
    keywords = [str(k) for k in keywords]
    match_authors = bool(meta.get("match_authors", False))
    select_code = str(meta.get("select", ""))
    sticky_val = meta.get("sticky")
    sticky = int(sticky_val) if sticky_val is not None else None
    user_ids_raw = meta.get("user_ids", [])
    if not isinstance(user_ids_raw, list):
        user_ids_raw = [user_ids_raw]
    user_ids = [int(u) for u in user_ids_raw if u]

    if select_code and len(select_code) > _MAX_SELECT_LEN:
        _LOG.warning("Fragment %s select snippet too long (%d chars), skipping",
                     path.name, len(select_code))
        select_code = ""

    return Fragment(
        path=path,
        type=ftype,
        order=order,
        channel=channel,
        keywords=keywords,
        match_authors=match_authors,
        select=select_code,
        content=body.strip(),
        sticky=sticky,
        user_ids=user_ids,
    )


# ---------------------------------------------------------------------------
# Selection logic
# ---------------------------------------------------------------------------


def execute_select(code: str, messages: list[dict], authors: list[str],
                   channel_id: str, combined: str) -> bool:
    """Safely execute a select snippet and return its boolean result."""
    dedented = textwrap.dedent(code).strip()
    lines = dedented.split("\n")
    indented = "\n".join("  " + line for line in lines)
    func_code = f"def _select(messages, authors, channel_id, combined):\n{indented}\n"

    local_ns: dict = {}
    global_ns = {"__builtins__": _SAFE_BUILTINS}

    try:
        exec(func_code, global_ns, local_ns)  # noqa: S102
        result = local_ns["_select"](messages, authors, channel_id, combined)
        return bool(result)
    except Exception as e:
        _LOG.warning("Select snippet failed: %s", e)
        return False


def matches_context(fragment: Fragment, messages: list[dict],
                    authors: list[str], channel_id: str) -> bool:
    """Evaluate whether a fragment should load given current context."""
    if fragment.type in ("common", "anchor"):
        return True

    if fragment.type == "channel":
        return fragment.channel == channel_id

    # Person fragments: check user IDs first (most reliable, no false positives)
    if fragment.type == "person" and fragment.user_ids:
        msg_author_ids = {m.get("author_id") for m in messages if m.get("author_id")}
        if msg_author_ids & set(fragment.user_ids):
            return True

    has_rules = bool(fragment.keywords) or bool(fragment.select)

    if not has_rules:
        return fragment.type == "person"

    if fragment.select:
        combined = " ".join(m.get("content", "") for m in messages).lower()
        return execute_select(fragment.select, messages, authors, channel_id, combined)

    combined = " ".join(m.get("content", "") for m in messages).lower()

    for kw in fragment.keywords:
        kw_lower = kw.lower()
        # Person fragments use word-boundary matching to avoid short-name false positives
        if fragment.type == "person":
            if re.search(r"\b" + re.escape(kw_lower) + r"\b", combined):
                return True
        else:
            if kw_lower in combined:
                return True
        if fragment.match_authors:
            for author in authors:
                if kw_lower in author:
                    return True

    return False


# ---------------------------------------------------------------------------
# Scanning and loading
# ---------------------------------------------------------------------------


def _load_people_dir(people_dir: Path) -> list[Fragment]:
    """Auto-load .md files from a people/ subdir as person fragments.

    Files with valid frontmatter are parsed normally. Files without are
    auto-derived as person fragments with keywords from the filename stem.
    """
    frags = []
    for f in people_dir.iterdir():
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            _LOG.warning("Failed to read people file %s: %s", f, e)
            continue

        meta, _ = parse_frontmatter(text)
        if meta is not None and meta.get("type") in VALID_TYPES:
            frag = parse_fragment(f)
            if frag is not None:
                frags.append(frag)
            continue

        # Auto-derive: no valid frontmatter -- treat whole file as person entry
        stem = f.stem
        parts = re.split(r"[_\-\s]+", stem)
        keywords = list(dict.fromkeys([stem] + [p for p in parts if p]))
        frags.append(Fragment(
            path=f,
            type="person",
            order=50,
            channel="",
            keywords=keywords,
            match_authors=True,
            select="",
            content=text.strip(),
            sticky=None,
        ))

    return frags


def scan_fragments(frag_dir: Path | None = None) -> list[Fragment]:
    """Scan directory for .md files with valid frontmatter."""
    d = frag_dir or FRAGMENTS_DIR
    if not d.exists():
        return []

    fragments = []
    for f in d.iterdir():
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        frag = parse_fragment(f)
        if frag is not None:
            fragments.append(frag)

    people_dir = d / "people"
    if people_dir.is_dir():
        fragments.extend(_load_people_dir(people_dir))

    return fragments


# How many turns a topic stays loaded after its keywords stop matching.
TOPIC_STICKY_TURNS: int = 8


def _load_topic_state(state_path: Path) -> dict[str, int]:
    """Load per-topic turn-since-last-match counters."""
    try:
        return json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_topic_state(state_path: Path, state: dict[str, int]) -> None:
    try:
        state_path.write_text(json.dumps(state))
    except OSError as e:
        _LOG.warning("Failed to save topic state: %s", e)


def load_fragments(
    channel_id: str,
    channel_name: str,
    messages: list[dict] | None = None,
    authors: list[str] | None = None,
    frag_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, str]:
    """Load and assemble all fragment sections for a channel.

    Returns dict with keys: "persons", "channel", "topics", "anchors".

    Topics use sticky loading: once a topic fragment matches, it stays loaded
    for TOPIC_STICKY_TURNS turns after its keywords stop matching. This keeps
    the system prompt prefix stable across turns, preserving Claude's cache.
    """
    msgs = messages or []
    auths = authors or [m.get("author", "").lower() for m in msgs]

    all_frags = scan_fragments(frag_dir)

    # Load per-channel topic state (turns since last keyword match)
    chan_dir = state_dir or channel_dir(channel_name)
    state_path = chan_dir / ".topic_state.json"
    topic_state = _load_topic_state(state_path)
    new_state: dict[str, int] = {}

    persons: list[Fragment] = []
    channel_frags: list[Fragment] = []
    common_frags: list[Fragment] = []
    topics: list[Fragment] = []
    anchors: list[Fragment] = []

    for frag in all_frags:
        if not frag.content:
            continue

        if frag.type == "topic":
            key = frag.path.name
            matched_now = matches_context(frag, msgs, auths, channel_id)
            if matched_now:
                new_state[key] = 0
                topics.append(frag)
            else:
                sticky_turns = frag.sticky if frag.sticky is not None else TOPIC_STICKY_TURNS
                turns_stale = topic_state.get(key, sticky_turns) + 1
                new_state[key] = turns_stale
                if turns_stale <= sticky_turns:
                    topics.append(frag)
            continue

        if not matches_context(frag, msgs, auths, channel_id):
            continue

        if frag.type == "person":
            persons.append(frag)
        elif frag.type == "channel":
            channel_frags.append(frag)
        elif frag.type == "common":
            common_frags.append(frag)
        elif frag.type == "anchor":
            anchors.append(frag)

    persons.sort(key=lambda f: f.order)
    common_frags.sort(key=lambda f: f.order)
    channel_frags.sort(key=lambda f: f.order)
    topics.sort(key=lambda f: f.order)
    anchors.sort(key=lambda f: f.order)

    _save_topic_state(state_path, new_state)

    result = {
        "persons": _format_persons(persons),
        "channel": _format_channel(common_frags, channel_frags, channel_name),
        "topics": _format_topics(topics),
        "anchors": _format_anchors(anchors),
    }

    _LOG.info(
        "Fragments: persons=%d, channel=%d, topics=%d, anchors=%d chars",
        len(result["persons"]), len(result["channel"]),
        len(result["topics"]), len(result["anchors"]),
    )

    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _join_contents(frags: list[Fragment]) -> str:
    return "\n\n---\n".join(f.content for f in frags)


def _format_persons(frags: list[Fragment]) -> str:
    if not frags:
        return ""
    content = _join_contents(frags)
    return f"\n\n---\nPEOPLE YOU KNOW:\n{content}\n---"


def _format_channel(common: list[Fragment], channel: list[Fragment],
                    channel_name: str) -> str:
    merged = sorted(common + channel, key=lambda f: f.order)

    sections = []
    for frag in merged:
        sections.append(f"--- {frag.path.name} ---\n{frag.content}")

    result = ""
    if sections:
        result = (
            "\n\n---\n"
            "CHANNEL INSTRUCTIONS (from /data/wendy/claude_fragments/ - you can edit these files):\n"
        )
        result += "\n".join(sections)
        result += "\n---"

    # No legacy CLAUDE.md fallback in v2 -- fragments are the system
    return result


def _format_topics(frags: list[Fragment]) -> str:
    if not frags:
        return ""
    content = _join_contents(frags)
    return f"\n\n---\nRELEVANT CONTEXT:\n{content}\n---"


def _format_anchors(frags: list[Fragment]) -> str:
    if not frags:
        return ""
    content = _join_contents(frags)
    return f"\n\n---\nBEHAVIORAL REMINDERS:\n{content}\n---"


# ---------------------------------------------------------------------------
# Message reading (from SQLite)
# ---------------------------------------------------------------------------


def get_recent_messages(channel_id: int, count: int = 8,
                       db_path: Path | None = None) -> list[dict]:
    """Read recent messages from SQLite for keyword matching."""
    conn = None
    try:
        conn = sqlite3.connect(str(db_path or DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT author_id, author_nickname, content
            FROM message_history
            WHERE channel_id = ?
              AND author_id != ?
              AND content IS NOT NULL
              AND content != ''
              AND content NOT LIKE '!%'
              AND content NOT LIKE '-%'
            ORDER BY message_id DESC
            LIMIT ?
            """,
            (channel_id, WENDY_BOT_ID, count),
        ).fetchall()

        return [
            {"author_id": row["author_id"], "author": row["author_nickname"], "content": row["content"]}
            for row in reversed(rows)
        ]
    except Exception as e:
        _LOG.warning("Failed to read recent messages: %s", e)
        return []
    finally:
        if conn:
            conn.close()
