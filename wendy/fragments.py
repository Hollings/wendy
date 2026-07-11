"""Fragment loading with YAML frontmatter.

Port of v1 fragment_loader.py -- nearly verbatim since it's already clean.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import textwrap
from pathlib import Path

import yaml

from .paths import FRAGMENTS_DIR, channel_dir

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
    description: str = ""
    behavioral: bool = False


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


# Parse cache: fragment files are read and parsed on every turn (the system
# prompt build and the nudge roster both scan the directory), and person files
# can grow large. Keyed by (mtime_ns, size) so runtime edits invalidate.
_PARSE_CACHE: dict[Path, tuple[tuple[int, int], Fragment | None]] = {}


def _cached(path: Path, parser) -> Fragment | None:
    """Run *parser(path)* with an (mtime_ns, size)-keyed cache."""
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return parser(path)
    hit = _PARSE_CACHE.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    frag = parser(path)
    _PARSE_CACHE[path] = (stamp, frag)
    return frag


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

    description = str(meta.get("description", ""))
    behavioral = bool(meta.get("behavioral", False))

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
        description=description,
        behavioral=behavioral,
    )


# ---------------------------------------------------------------------------
# Selection logic
# ---------------------------------------------------------------------------


def execute_select(code: str, messages: list[dict], authors: list[str],
                   channel_id: str, combined: str) -> bool:
    """Safely execute a select snippet and return its boolean result."""
    dedented = textwrap.dedent(code).strip()

    # Reject dunder access -- legitimate select expressions never need to
    # reach __class__, __bases__, __subclasses__, etc. and these are the
    # primary vectors for escaping a restricted exec() sandbox.
    if "__" in dedented:
        _LOG.warning("Select snippet contains dunder access, refusing to execute")
        return False

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


def _parse_people_file(f: Path) -> Fragment | None:
    """Parse one people/ file: frontmatter if valid, else auto-derive.

    Auto-derived files become person fragments with keywords from the
    filename stem and match_authors enabled.
    """
    try:
        text = f.read_text(encoding="utf-8")
    except OSError as e:
        _LOG.warning("Failed to read people file %s: %s", f, e)
        return None

    meta, _ = parse_frontmatter(text)
    if meta is not None and meta.get("type") in VALID_TYPES:
        return parse_fragment(f)

    # Auto-derive: no valid frontmatter -- treat whole file as person entry
    stem = f.stem
    parts = re.split(r"[_\-\s]+", stem)
    keywords = list(dict.fromkeys([stem] + [p for p in parts if p]))
    return Fragment(
        path=f,
        type="person",
        order=50,
        channel="",
        keywords=keywords,
        match_authors=True,
        select="",
        content=text.strip(),
        sticky=None,
    )


def _load_people_dir(people_dir: Path) -> list[Fragment]:
    """Auto-load .md files from a people/ subdir as person fragments."""
    frags = []
    for f in people_dir.iterdir():
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        frag = _cached(f, _parse_people_file)
        if frag is not None:
            frags.append(frag)
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
        frag = _cached(f, parse_fragment)
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

    "persons" is always empty string -- person context reaches Wendy via the
    nudge roster line (see get_present_context()), not the system prompt.

    Topics only include behavioral: true fragments -- non-behavioral topics
    are surfaced through the nudge roster instead. Behavioral topics use
    sticky loading to keep the system prompt stable.
    """
    msgs = messages or []
    auths = authors or [m.get("author", "").lower() for m in msgs]

    all_frags = scan_fragments(frag_dir)

    # Load per-channel topic state (only used for behavioral topics now)
    chan_dir = state_dir or channel_dir(channel_name)
    state_path = chan_dir / ".topic_state.json"
    topic_state = _load_topic_state(state_path)
    new_state: dict[str, int] = {}

    channel_frags: list[Fragment] = []
    common_frags: list[Fragment] = []
    topics: list[Fragment] = []
    anchors: list[Fragment] = []

    for frag in all_frags:
        if not frag.content:
            continue

        if frag.type == "topic":
            # Only behavioral topics go into the system prompt inline
            if not frag.behavioral:
                continue
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

        # Person fragments are injected as synthetic messages, not system prompt
        if frag.type == "person":
            continue
        elif frag.type == "channel":
            channel_frags.append(frag)
        elif frag.type == "common":
            common_frags.append(frag)
        elif frag.type == "anchor":
            anchors.append(frag)

    common_frags.sort(key=lambda f: f.order)
    channel_frags.sort(key=lambda f: f.order)
    topics.sort(key=lambda f: f.order)
    anchors.sort(key=lambda f: f.order)

    _save_topic_state(state_path, new_state)

    result = {
        "persons": "",  # Persons now injected via synthetic messages
        "channel": _format_channel(common_frags, channel_frags, channel_name),
        "topics": _format_topics(topics),
        "anchors": _format_anchors(anchors),
    }

    _LOG.info(
        "Fragments: channel=%d, topics=%d, anchors=%d chars",
        len(result["channel"]), len(result["topics"]), len(result["anchors"]),
    )

    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _join_contents(frags: list[Fragment]) -> str:
    return "\n\n---\n".join(f.content for f in frags)


def _format_channel(common: list[Fragment], channel: list[Fragment],
                    channel_name: str) -> str:
    merged = sorted(common + channel, key=lambda f: f.order)

    sections = []
    for frag in merged:
        first, _, rest = frag.content.partition("\n")
        if first.startswith("#"):
            labeled = f"{first} ({frag.path.name})\n{rest}" if rest else f"{first} ({frag.path.name})"
        else:
            labeled = f"### {frag.path.name}\n{frag.content}"
        sections.append(labeled)

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
    return f"\n\n---\n{content}\n---"


# ---------------------------------------------------------------------------
# Present-context roster (person/topic awareness for the nudge prompt)
# ---------------------------------------------------------------------------


def get_present_context(
    messages: list[dict],
    channel_id: str = "",
    frag_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Return (person names, non-behavioral topic filenames) matching *messages*.

    Replaces the old synthetic context-introduction system: instead of
    stateful one-shot intro messages (which the msgs display filter swallowed
    anyway), the nudge prompt carries a one-line roster built from this every
    turn. behavioral: true topics stay in the system prompt and are excluded.
    """
    authors = [m.get("author", "").lower() for m in messages]
    people: list[str] = []
    topics: list[str] = []

    for frag in scan_fragments(frag_dir):
        if frag.behavioral or not frag.content:
            continue
        if frag.type == "person":
            if matches_context(frag, messages, authors, channel_id):
                people.append(frag.path.stem)
        elif frag.type == "topic":
            if matches_context(frag, messages, authors, channel_id):
                topics.append(frag.path.name)

    return sorted(set(people)), sorted(set(topics))


# ---------------------------------------------------------------------------
# Message reading (from SQLite)
# ---------------------------------------------------------------------------


def get_recent_messages(channel_id: int, count: int = 8) -> list[dict]:
    """Read recent real messages from SQLite for keyword matching.

    Delegates to ``state_manager.get_recent_messages()`` which excludes
    synthetic messages so that injected context never triggers keyword
    matches or fragment selection.
    """
    from .state import state as state_manager

    return state_manager.get_recent_messages(channel_id, limit=count)
