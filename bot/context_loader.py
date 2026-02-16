"""Dynamic context loading for Wendy bot.

Selects relevant topic files based on recent Discord messages, using either
a Haiku Claude Code call for semantic selection or a keyword-based fallback.

Messages are read directly from SQLite (no proxy call, no last_seen side effects).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
from pathlib import Path

from .paths import DB_PATH, PROMPTS_DIR

_LOG = logging.getLogger(__name__)

# Wendy's own Discord user ID - filtered from message queries
WENDY_USER_ID = 771821437199581204

# Config for Haiku topic selection
HAIKU_TIMEOUT_SECONDS = 15
HAIKU_MODEL = "claude-haiku-4-5-20251001"


def get_recent_messages(channel_id: int, count: int = 8, db_path: Path | None = None) -> list[dict]:
    """Read recent messages from SQLite directly (no last_seen update).

    Uses a direct sqlite3 connection (read-only) to avoid importing
    state_manager and its Python 3.11+ dependencies.

    Args:
        channel_id: Discord channel ID.
        count: Maximum number of messages to return.
        db_path: Override database path (for testing).

    Returns:
        List of {"author": str, "content": str} dicts, oldest-first.
    """
    try:
        conn = sqlite3.connect(str(db_path or DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT author_nickname, content
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
            (channel_id, WENDY_USER_ID, count),
        ).fetchall()
        conn.close()

        # Reverse to get oldest-first order
        return [
            {"author": row["author_nickname"], "content": row["content"]}
            for row in reversed(rows)
        ]
    except Exception as e:
        _LOG.warning("Failed to read recent messages: %s", e)
        return []


def load_manifest() -> dict | None:
    """Load the topic manifest from the prompts directory.

    Returns:
        Parsed manifest dict, or None if missing/invalid.
    """
    manifest_path = PROMPTS_DIR / "manifest.json"
    try:
        return json.loads(manifest_path.read_text())
    except FileNotFoundError:
        _LOG.warning("Manifest not found at %s", manifest_path)
        return None
    except (json.JSONDecodeError, OSError) as e:
        _LOG.warning("Failed to load manifest: %s", e)
        return None


async def select_topics(
    messages: list[dict],
    manifest: dict,
    cli_path: str,
) -> list[str]:
    """Use Haiku to semantically select relevant topics.

    Args:
        messages: Recent messages from the channel.
        manifest: Parsed manifest dict with topics.
        cli_path: Path to the claude CLI executable.

    Returns:
        List of topic filenames selected by Haiku, or [] on failure.
    """
    if not messages:
        return []

    topics = manifest.get("topics", {})
    if not topics:
        return []

    # Build the selection prompt
    msg_text = "\n".join(
        f"  {m['author']}: {m['content']}" for m in messages
    )
    topic_list = "\n".join(
        f"  - {fname}: {info['description']}"
        for fname, info in topics.items()
    )

    prompt = (
        "Given these recent Discord messages:\n"
        f"{msg_text}\n\n"
        "Which of these topic files are relevant? Return ONLY the filenames "
        "(one per line), or NONE if no topics match.\n"
        f"{topic_list}"
    )

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                cli_path,
                "-p", prompt,
                "--output-format", "text",
                "--model", HAIKU_MODEL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=HAIKU_TIMEOUT_SECONDS,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=HAIKU_TIMEOUT_SECONDS,
        )

        output = stdout.decode("utf-8").strip()
        _LOG.info("Haiku topic selection output: %r", output)

        if not output or output.upper() == "NONE":
            return []

        # Validate returned filenames against manifest keys
        valid_topics = set(topics.keys())
        selected = []
        for line in output.split("\n"):
            fname = line.strip().strip("- ")
            if fname in valid_topics:
                selected.append(fname)

        _LOG.info("Topic selection result: %s", selected)
        return selected

    except TimeoutError:
        _LOG.warning("Haiku topic selection timed out after %ds", HAIKU_TIMEOUT_SECONDS)
        return []
    except Exception as e:
        _LOG.warning("Haiku topic selection failed: %s", e)
        return []


def keyword_fallback(messages: list[dict], manifest: dict) -> list[str]:
    """Simple keyword-based topic selection fallback.

    Args:
        messages: Recent messages from the channel.
        manifest: Parsed manifest dict with topics.

    Returns:
        List of matching topic filenames.
    """
    topics = manifest.get("topics", {})
    if not topics or not messages:
        return []

    # Join all message content into one lowercase string
    combined = " ".join(m["content"] for m in messages).lower()

    matched = []
    for fname, info in topics.items():
        keywords = info.get("keywords", [])
        for kw in keywords:
            if kw.lower() in combined:
                matched.append(fname)
                break

    _LOG.info("Keyword fallback result: %s", matched)
    return matched


def load_topic_files(filenames: list[str]) -> str:
    """Read and concatenate topic .md files from the prompts directory.

    Args:
        filenames: List of filenames relative to PROMPTS_DIR.

    Returns:
        Concatenated content with separators.
    """
    parts = []
    for fname in filenames:
        fpath = PROMPTS_DIR / fname
        try:
            content = fpath.read_text().strip()
            if content:
                parts.append(content)
        except FileNotFoundError:
            _LOG.warning("Topic file not found: %s", fpath)
        except OSError as e:
            _LOG.warning("Failed to read topic file %s: %s", fpath, e)

    return "\n\n---\n".join(parts)


async def build_dynamic_context(
    channel_id: int,
    cli_path: str,
) -> dict[str, str] | None:
    """Build dynamic context sections for the system prompt.

    Main entry point. Returns a dict with three sections:
    - always_top: People files (always included)
    - topics: Haiku-selected or keyword-fallback topic files
    - always_bottom: Behavioral anchors (always included)

    Args:
        channel_id: Discord channel ID.
        cli_path: Path to the claude CLI executable.

    Returns:
        Dict with context sections, or None on total failure.
    """
    manifest = load_manifest()
    if manifest is None:
        return None

    result = {"always_top": "", "topics": "", "always_bottom": ""}

    # Always-inject files
    always_top_files = manifest.get("always_top", [])
    if always_top_files:
        content = load_topic_files(always_top_files)
        if content:
            result["always_top"] = f"\n\n---\nPEOPLE YOU KNOW:\n{content}\n---"

    always_bottom_files = manifest.get("always_bottom", [])
    if always_bottom_files:
        content = load_topic_files(always_bottom_files)
        if content:
            result["always_bottom"] = f"\n\n---\nBEHAVIORAL REMINDERS:\n{content}\n---"

    # Topic selection
    messages = get_recent_messages(channel_id)

    if messages:
        selected = await select_topics(messages, manifest, cli_path)

        # Fall back to keywords if Haiku returned nothing
        if not selected:
            selected = keyword_fallback(messages, manifest)

        if selected:
            content = load_topic_files(selected)
            if content:
                result["topics"] = f"\n\n---\nRELEVANT CONTEXT:\n{content}\n---"

    _LOG.info(
        "Dynamic context: always_top=%d chars, topics=%d chars, always_bottom=%d chars",
        len(result["always_top"]),
        len(result["topics"]),
        len(result["always_bottom"]),
    )

    return result


def setup_prompts_dir() -> None:
    """Seed prompt files from /app/config/prompts/ to PROMPTS_DIR.

    Only copies files that don't already exist, preserving Wendy's edits.
    """
    src_dir = Path("/app/config/prompts")
    if not src_dir.exists():
        _LOG.info("No source prompts dir at %s, skipping seed", src_dir)
        return

    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

    for src_file in src_dir.rglob("*"):
        if not src_file.is_file():
            continue

        rel_path = src_file.relative_to(src_dir)
        dest_file = PROMPTS_DIR / rel_path

        if dest_file.exists():
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        _LOG.info("Seeded prompt file: %s", rel_path)
