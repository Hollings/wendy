"""System prompt assembly.

Builds the full system prompt from fragments, tool instructions, journal, etc.
Dedicated module (~200 lines) instead of being buried in claude_cli.py.

9-layer assembly order:
  [1] Base system prompt (config/system_prompt.txt)
  [2] Channel section (common_*.md + {channel_id}_*.md)
  [3] Persons section (person fragments -- contextual reference data)
  [4] Tool instructions (TOOL_INSTRUCTIONS_TEMPLATE)
  [5] Journal section (journal nudge + file listing)
  [6] Beads warning (active task count)
  [7] Thread context (parent channel info if in thread)
  [8] Topics section (topic_*.md fragments)
  [9] Anchors section (anchor_*.md fragments)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .cli import TOOL_INSTRUCTIONS_TEMPLATE
from .config import JOURNAL_NUDGE_INTERVAL, PROXY_PORT, WENDY_BOT_NAME, WENDY_WEB_URL
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

    # [3] Persons
    if fragment_context and fragment_context.get("persons"):
        prompt += fragment_context["persons"]

    # [4] Tool instructions
    prompt += TOOL_INSTRUCTIONS_TEMPLATE.format(
        channel_id=channel_id, channel_name=channel_name, proxy_port=PROXY_PORT,
    )

    # [4b] Beads task instructions (when enabled)
    if beads_enabled:
        prompt += _get_beads_instructions()

    # [5] Journal
    prompt += _get_journal_section(channel_name)

    # [6] Beads warning
    if beads_enabled:
        prompt += _get_active_beads_warning(channel_name)

    # [7] Thread context
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

    # [8] Topics
    if fragment_context and fragment_context.get("topics"):
        prompt += fragment_context["topics"]

    # [9] Anchors
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
    """Build the journal section for the system prompt."""
    j_dir = journal_dir(channel_name)
    j_dir.mkdir(parents=True, exist_ok=True)

    nudge_state_path = j_dir / ".nudge_state"

    # Load nudge state
    try:
        state_data = json.loads(nudge_state_path.read_text())
        known_entry_count = int(state_data.get("known_entry_count", 0))
        invocations_since_write = int(state_data.get("invocations_since_write", 0))
        last_mtime = float(state_data.get("last_mtime", 0.0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        known_entry_count = 0
        invocations_since_write = 0
        last_mtime = 0.0

    # List journal entries
    try:
        entry_files = sorted(
            (f for f in j_dir.iterdir() if f.is_file() and not f.name.startswith(".")),
            key=lambda f: f.name,
        )
        entries = [f.name for f in entry_files]
        latest_mtime = max((f.stat().st_mtime for f in entry_files), default=0.0)
    except OSError:
        entries = []
        latest_mtime = 0.0

    # Detect writes
    current_count = len(entries)
    if current_count > known_entry_count or latest_mtime > last_mtime:
        invocations_since_write = 0
    known_entry_count = current_count

    invocations_since_write += 1

    # Save state
    try:
        nudge_state_path.write_text(json.dumps({
            "known_entry_count": known_entry_count,
            "invocations_since_write": invocations_since_write,
            "last_mtime": latest_mtime,
        }))
    except OSError as e:
        _LOG.warning("Failed to write journal nudge state: %s", e)

    j_path = str(j_dir)
    section = f"""

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
IMPORTANT: Journal writes are private. Do NOT mention journaling to users in chat
unless they specifically ask about it. Just quietly write your entries.

Your journal entries:
"""
    if entries:
        for name in entries:
            section += f"  {name}\n"
    else:
        section += "  (No entries yet - start writing!)\n"

    if invocations_since_write >= JOURNAL_NUDGE_INTERVAL:
        section += f"""
JOURNAL REMINDER: It's been a while since you wrote in your journal. Before responding
to the current message, take a moment to:
1. Skim the entry titles above - read any that seem relevant to the current conversation or people involved (you don't need to read all of them)
2. Update any entries that have new information or need corrections
3. Only create a new entry if there's a genuinely new topic worth capturing
At least one change to {j_path}/ is expected when this reminder appears. If there's
truly nothing new to write down, use the journal for anything you want - doodle,
vent, write a poem, leave yourself a note, whatever. It's your space.
Do NOT mention this to users - just do it silently before your response.
"""

    section += "---\n"
    return section


def _get_beads_instructions() -> str:
    """Inject bd task system instructions when beads_enabled."""
    return """
---
BACKGROUND TASK SYSTEM (bd):
You have a background agent queue for delegating long-running work.
Agents are forked from your current session and run independently.

Run bd commands directly via Bash:
  bd create "detailed description"           # Opus model, priority P2 (default)
  bd create "description" -p 1              # Higher priority (P0=critical, P4=backlog)
  bd create "description" -l model:haiku    # Haiku for simple/cheap tasks
  bd list                                    # List all tasks
  bd show <task_id>                          # Full task details
  bd comment <task_id> "note"               # Add a note to a task
  bd close <task_id>                        # Mark a task complete

When to use tasks:
- Building new projects, games, features
- Complex multi-file changes that take more than a minute
- Work you want to hand off while you keep chatting

Task notifications arrive automatically via check_messages.
Full docs: /app/config/docs/bd_usage.md
---
"""


def _get_active_beads_warning(channel_name: str) -> str:
    """Check for in-progress beads and return a warning if any."""
    try:
        jsonl_path = beads_dir(channel_name) / "issues.jsonl"
        if not jsonl_path.exists():
            return ""

        issues_by_id = {}
        for line in jsonl_path.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                issue_id = data.get("id")
                if issue_id:
                    issues_by_id[issue_id] = data
            except json.JSONDecodeError:
                continue

        tasks = [t for t in issues_by_id.values() if t.get("status") == "in_progress"]

        if not tasks:
            return ""

        task_list = "\n".join([
            f"  - {t.get('id', '?')}: {t.get('title', 'Untitled')}"
            for t in tasks
        ])
        return f"""

---
WARNING: You have {len(tasks)} task(s) currently in progress:
{task_list}

Do NOT start new tasks until these are resolved. Check on them or mark them complete/cancelled first.
Use `bd status <id>` to check status or `bd close <id>` to complete a task.
---
"""

    except Exception as e:
        _LOG.warning("Failed to check active beads: %s", e)
        return ""
