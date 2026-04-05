"""System prompt assembly.

Builds the full system prompt from fragments, tool instructions, journal, etc.
Dedicated module (~200 lines) instead of being buried in claude_cli.py.

Assembly order:
  [1] Base system prompt (config/system_prompt.txt)
  [2] Channel section (common_*.md + {channel_id}_*.md)
  [3] Tool instructions (TOOL_INSTRUCTIONS_TEMPLATE)
  [4] Journal section (file listing only)
  [5] Thread context (parent channel info if in thread)
  [6] Topics section (behavioral: true topic fragments only)
  [7] Anchors section (anchor_*.md fragments)

Person fragments and non-behavioral topic fragments are injected as synthetic
messages before each CLI invocation (see discord_client.py) rather than
inline in the system prompt. This keeps the system prompt stable across turns
so Claude's cache prefix is not invalidated by who is in the conversation.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .cli import TOOL_INSTRUCTIONS_TEMPLATE
from .config import PROXY_PORT, WENDY_BOT_NAME, WENDY_WEB_URL
from .fragments import get_recent_messages, load_fragments
from .paths import beads_dir, journal_dir

_LOG = logging.getLogger(__name__)


def build_system_prompt(channel_id: int, channel_config: dict) -> str:
    """Build the complete system prompt for a channel."""
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))
    mode = channel_config.get("mode", "full")
    beads_enabled = channel_config.get("beads_enabled", False)

    is_thread = channel_config.get("_is_thread", False)
    parent_folder = channel_config.get("_parent_folder")
    thread_name = channel_config.get("_thread_name")
    thread_folder = channel_config.get("_folder") if is_thread else None
    parent_channel_id = int(channel_config.get("_parent_channel_id", 0)) or None if is_thread else None

    # [1] Base system prompt
    prompt = _get_base_system_prompt(channel_name, mode)

    # Load fragment context
    fragment_context = _load_fragment_context(channel_id, channel_name, parent_channel_id)

    # [2] Channel
    if fragment_context and fragment_context.get("channel"):
        prompt += fragment_context["channel"]

    # [3] Tool instructions
    prompt += TOOL_INSTRUCTIONS_TEMPLATE.format(
        channel_id=channel_id, channel_name=channel_name, proxy_port=PROXY_PORT,
    )

    # [3b] Beads task instructions (when enabled)
    if beads_enabled:
        prompt += _get_beads_instructions()

    # [4] Journal
    prompt += _get_journal_section(channel_name)

    # [5] Thread context
    if is_thread and thread_name and thread_folder and parent_folder:
        prompt += f"""
---
THREAD CONTEXT:
You are in a Discord thread called "{thread_name}" (not the main channel).
This thread has its own separate conversation history and session.
Messages you send here stay in this thread.
Your workspace: /data/wendy/channels/{thread_folder}/
Parent channel workspace: /data/wendy/channels/{parent_folder}/ (read-only reference)
---
"""

    # [6] Topics (behavioral: true only -- others injected as synthetic messages)
    if fragment_context and fragment_context.get("topics"):
        prompt += fragment_context["topics"]

    # [7] Anchors
    if fragment_context and fragment_context.get("anchors"):
        prompt += fragment_context["anchors"]

    return prompt


def _get_base_system_prompt(channel_name: str, mode: str = "full") -> str:
    """Load and process the base system prompt file."""
    system_prompt_file = os.getenv("SYSTEM_PROMPT_FILE", "/app/config/system_prompt.txt")
    if not Path(system_prompt_file).exists():
        return ""

    try:
        content = Path(system_prompt_file).read_text().strip()
        content = content.replace("{folder}", channel_name)
        content = content.replace("{bot_name}", WENDY_BOT_NAME)
        content = content.replace("{web_url}", WENDY_WEB_URL)

        if mode == "chat":
            import re as _re
            content = _re.sub(
                r"\n?<!-- FULL_ONLY_START -->.*?<!-- FULL_ONLY_END -->\n?",
                "",
                content,
                flags=_re.DOTALL,
            )

        return content
    except Exception as e:
        _LOG.warning("Failed to read system prompt file: %s", e)
        return ""


def _load_fragment_context(channel_id: int, channel_name: str,
                           parent_channel_id: int | None = None) -> dict[str, str] | None:
    """Load all fragment sections for the system prompt."""
    fragment_id = str(parent_channel_id) if parent_channel_id else str(channel_id)

    try:
        messages = get_recent_messages(channel_id)
        authors = [m.get("author", "").lower() for m in messages]

        return load_fragments(
            channel_id=fragment_id,
            channel_name=channel_name,
            messages=messages,
            authors=authors,
        )
    except Exception as e:
        _LOG.warning("Fragment context loading failed: %s", e)
        return None


def _get_journal_section(channel_name: str) -> str:
    """Build the static journal section for the system prompt (instructions only, no file listing)."""
    j_dir = journal_dir(channel_name)
    j_dir.mkdir(parents=True, exist_ok=True)
    j_path = str(j_dir)
    return f"""

---
JOURNAL (your long-term memory):
Your journal is at {j_path}/
This is your persistent memory across conversations. Use it strategically:
- READ existing entries before writing new ones - build on what you already know
- UPDATE entries when you learn something new about an existing topic
- CREATE new entries only for genuinely new topics or significant experiences
- DELETE or consolidate entries that are redundant or no longer useful
Filenames should include a date and descriptive name, e.g.: 2026-02-05_learned-about-docker-networks.md
Favor quality over quantity - a few well-maintained entries are better than many shallow ones.
Since you get 15 minutes of personal free time daily, a personal wishlist or ideas list in the journal is a good investment -- jot down things you've been wanting to explore so you're ready when the break arrives.
IMPORTANT: Journal writes are private. Do NOT mention journaling to users in chat
unless they specifically ask about it. Just quietly write your entries.
---
"""


def get_journal_listing_for_nudge(channel_name: str) -> str:
    """Return a compact journal listing for the nudge prompt, or empty string if no entries."""
    j_dir = journal_dir(channel_name)
    try:
        entries = sorted(
            f.name
            for f in j_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )
    except OSError:
        return ""

    if not entries:
        return ""

    names = ", ".join(entries)
    return f"Journal entries ({len(entries)} files): {names}"


def _get_beads_instructions() -> str:
    """Inject bd task system instructions when beads_enabled."""
    return """
---
BACKGROUND TASK SYSTEM (bd):
`bd` is your background agent queue. Use `bd create "description"` to fork your current session and run longer work independently. You'll be notified via check_messages when tasks finish.
Full reference: /app/config/docs/bd_usage.md
---
"""


def get_beads_warning_for_nudge(channel_name: str) -> str:
    """Return a compact beads warning for the nudge prompt, or empty string if none active."""
    import subprocess
    from .paths import channel_dir

    try:
        bd_dir = beads_dir(channel_name)
        if not (bd_dir / "config.yaml").exists():
            return ""

        result = subprocess.run(
            ["bd", "list", "--status", "in_progress", "--json"],
            capture_output=True, text=True, timeout=5,
            cwd=str(channel_dir(channel_name)),
            env={**os.environ, "BEADS_DIR": str(bd_dir)},
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""

        active = json.loads(result.stdout)
        if not active:
            return ""

        task_parts = ", ".join(
            f"{t.get('id', '?')} '{t.get('title', 'Untitled')}'" for t in active
        )
        return f"[{len(active)} active bead(s): {task_parts}]"

    except Exception as e:
        _LOG.warning("Failed to check active beads: %s", e)
        return ""
