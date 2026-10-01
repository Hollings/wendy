"""System prompt assembly.

Builds the full system prompt from fragments, tool instructions, journal, etc.
Dedicated module (~200 lines) instead of being buried in claude_cli.py.

Assembly order:
  [1] Base system prompt (config/system_prompt.txt)
  [2] Channel section (common_*.md + {channel_id}_*.md)
  [3] Tool instructions (TOOL_INSTRUCTIONS_TEMPLATE)
  [4] Static memory policy (listing is in the per-turn nudge)
  [5] Thread context (parent channel info if in thread)
  [6] Topics section (behavioral: true topic fragments only)
  [7] Anchors section (anchor_*.md fragments)

Person fragments and non-behavioral topic fragments are surfaced as a compact
roster line in the per-turn nudge prompt (get_context_roster_for_nudge) rather
than inline in the system prompt. This keeps the system prompt stable across
turns so Claude's cache prefix is not invalidated by who is in the
conversation.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .cli import TOOL_INSTRUCTIONS_TEMPLATE
from .config import PROXY_PORT, WENDY_BOT_NAME, WENDY_PUBLIC_URL
from .fragments import get_recent_messages, load_fragments
from .paths import journal_dir

_LOG = logging.getLogger(__name__)

def build_system_prompt(channel_id: int, channel_config: dict) -> str:
    """Build the complete system prompt for a channel."""
    channel_name = channel_config.get("_folder", channel_config.get("name", "default"))
    # Default to "chat" (limited tools): configs always set mode explicitly, so
    # this default only applies to unconfigured channels (e.g. a bot mention in
    # a non-whitelisted channel) -- those must not get full coding tools.
    mode = channel_config.get("mode", "chat")
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

    # [6] Topics (behavioral: true only -- others listed in the per-turn context roster)
    if fragment_context and fragment_context.get("topics"):
        prompt += fragment_context["topics"]

    # [7] Anchors
    if fragment_context and fragment_context.get("anchors"):
        prompt += fragment_context["anchors"]

    _LOG.info("Prompt channel=%s chars=%d", channel_id, len(prompt))
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
        content = content.replace("{web_url}", WENDY_PUBLIC_URL)

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
Your journal is at {j_path}/; people profiles are in /data/wendy/claude_fragments/people/.
Before asking for forgotten context, search these notes. Grep long files rather
than loading everything. Keep profiles compact: who they are, their current
situation, and how to interact. Put detailed events and lessons in dated journal
entries, e.g. 2026-02-05_docker-networks.md.
Read before updating; add only useful new information and preserve meaningful
history when consolidating. Don't delete existing notes or manufacture memories
to satisfy a reminder. If nothing new is worth saving, skip the write.
Memory maintenance is private; don't announce it unless asked.
---
"""


def get_journal_listing_for_nudge(channel_name: str) -> str:
    """Return a compact journal listing for the nudge prompt, or empty string if no entries."""
    j_dir = journal_dir(channel_name)
    try:
        files = [f for f in j_dir.iterdir() if f.is_file() and not f.name.startswith(".")]
        total = len(files)
        entries = [f.name[:120] for f in sorted(files, key=lambda f: (f.stat().st_mtime, f.name), reverse=True)[:12]]
    except OSError:
        return ""

    if not entries:
        return ""

    names = ", ".join(entries)
    return f"Journal entries ({total} files; up to 12 recent): {names}. Search {j_dir}/ for older notes."


def _get_beads_instructions() -> str:
    """Small, always-present command guide for BD-enabled conversations."""
    return """
---
BACKGROUND TASKS (wtask):
wtask start "title" -d "goal, exact paths, constraints, verification" --model opus
wtask models | wtask list | wtask show ID | wtask result ID
wtask tell ID "correction"  # queued delivery; show reports explicit acknowledgment
wtask stop ID "reason"     # stop while preserving files, logs and checkpoints
wtask resume ID            # continue saved attempt; no additional model slot
wtask retry ID             # new attempt; counts against its model quota
wtask model ID fable       # queued/stopped tasks only; changes model explicitly
Workers receive an immutable conversation excerpt and your brief. For a large
spec, save it first and reference the path. Use tell for corrections; BD comments
are historical notes, not worker messages. Questions/results return here
(the originating thread is preserved). Answer a question with tell, then resume.
Shared workspace: one worker per channel. While it runs, send edits via tell or
stop it before editing the same files yourself. Never discard unfinished files.
Global model quotas are enforced. Check wtask models for current models, limits,
remaining slots and reset times. A quota-blocked task stays queued; explicitly change models if appropriate.
Review artifacts/verification in result before announcing success or publishing.
The bd command is deprecated and only prints a warning. Use wtask for all task
operations; specify dependencies with wtask start ... --after TASK_ID (repeatable).
Do not bypass the warning with another BD executable or direct database edits.
Full reference: /app/config/docs/bd_usage.md
---
"""


def get_context_roster_for_nudge(channel_id: int) -> str:
    """Compact context roster for the nudge prompt, or empty string.

    Lists people in the recent conversation who have saved profiles, and any
    matching non-behavioral topic notes. One line each, rebuilt every turn --
    no state files, no synthetic messages.
    """
    from .fragments import get_present_context, get_recent_messages

    try:
        messages = get_recent_messages(channel_id)
        people, topics = get_present_context(messages, channel_id=str(channel_id))
    except Exception as e:
        _LOG.warning("Context roster failed: %s", e)
        return ""

    lines = []
    if people:
        lines.append(
            f"[People here with saved profiles: {', '.join(people)} -- read "
            f"/data/wendy/claude_fragments/people/<name>.md if you need background]"
        )
    if topics:
        lines.append(
            f"[Possibly relevant notes in /data/wendy/claude_fragments/: {', '.join(topics)}]"
        )
    return "\n".join(lines)


def get_beads_warning_for_nudge(channel_name: str) -> str:
    """Live task and quota feedback; no external BD subprocess on every turn."""
    from .task_store import TaskStore

    try:
        store = TaskStore()
        all_tasks = store.list(channel_name)
        active = [t for t in all_tasks if t['phase'] not in ('succeeded', 'failed', 'stopped', 'cancelled')]
        tasks = active[:8]
        history_count = len(all_tasks) - len(active)
        policy = store.models()
        quotas = [f"{m['name']}: {m['remaining']}/{m['limit']} remaining"
                  for m in policy['models'] if m['limit'] is not None]
        rows = [f"{t['bd_id']} {t['phase']}: {t['title'][:120]}" for t in tasks]
        return ('[Background tasks: ' + ('; '.join(rows) or 'none pending') +
                f'. {max(0, len(active) - len(tasks))} more active; {history_count} historical (wtask list). Model quotas: ' + ('; '.join(quotas) or 'unlimited') +
                '; reset ' + policy['resets_at'] + '. Use wtask show/models for details.]')
    except Exception:
        _LOG.warning('Task status unavailable', exc_info=True)
        return '[Task status unavailable; use wtask models/list before starting work.]'
