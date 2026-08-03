"""Laurels -- ambient recognition from the channel.

Inspired by Steve Yegge's "Laurels" system: posts of Wendy's that people
spontaneously loved (a reaction pile-on) get surfaced back to her so she
knows her work landed.

Deliberately designed to have no work attached, so there is nothing to
farm and nothing to optimize for:
- laurels never wake her and never arrive as notifications
- they carry no action items, priorities, or follow-ups
- they appear only in the system prompt, so she sees them for the whole
  session -- a satisfying message, nothing more

Reaction rows are recorded by discord_client's raw-reaction handlers into
the ``laurel_reactions`` table (state.py); this module aggregates them
into the prompt section.
"""
from __future__ import annotations

import logging
import time

from .config import LAUREL_MAX_SHOWN, LAUREL_THRESHOLD, LAUREL_WINDOW_DAYS
from .state import StateManager, state

_LOG = logging.getLogger(__name__)

_EXCERPT_MAX = 120


def _excerpt(content: str | None) -> str:
    """Collapse whitespace and truncate message content for a one-line display."""
    if not content or not content.strip():
        return "(no text -- an image or file post)"
    text = " ".join(content.split())
    if len(text) > _EXCERPT_MAX:
        return text[: _EXCERPT_MAX - 3] + "..."
    return text


def _format_line(entry: dict) -> str:
    """Render one laurel as a single prompt line."""
    ts = entry.get("message_ts") or entry["latest_at"]
    date = time.strftime("%b %d", time.gmtime(ts))
    emoji_parts = ", ".join(
        f"{emoji} x{count} ({reactors})" if reactors else f"{emoji} x{count}"
        for emoji, count, reactors in entry["emojis"]
    )
    return f'- {date}: "{_excerpt(entry.get("content"))}" -- {emoji_parts}'


def get_laurels_section(channel_ids: list[int], sm: StateManager | None = None) -> str:
    """Build the LAURELS system prompt section, or "" when there are none.

    *channel_ids* is the current channel plus (for threads) the parent, so a
    thread session still feels the glow from praise in the main channel.
    """
    sm = sm or state
    since_ts = int(time.time()) - LAUREL_WINDOW_DAYS * 86400
    try:
        rows = sm.get_laurels(
            channel_ids, LAUREL_THRESHOLD, since_ts, limit=LAUREL_MAX_SHOWN * 4,
        )
    except Exception as e:
        _LOG.warning("Laurel lookup failed: %s", e)
        return ""
    if not rows:
        return ""

    # Collapse per message: a post with several qualifying emojis is one
    # laurel with a combined reaction summary, not several entries.
    by_msg: dict[int, dict] = {}
    for r in rows:
        entry = by_msg.setdefault(r["message_id"], {
            "content": r["content"],
            "message_ts": r["message_ts"],
            "latest_at": 0,
            "emojis": [],
        })
        entry["emojis"].append((r["emoji"], r["count"], r["reactors"]))
        entry["latest_at"] = max(entry["latest_at"], r["latest_at"])

    ordered = sorted(by_msg.values(), key=lambda e: e["latest_at"], reverse=True)
    lines = "\n".join(_format_line(e) for e in ordered[:LAUREL_MAX_SHOWN])

    return f"""
---
LAURELS:
Recent posts of yours that people spontaneously loved -- enough reactions piled
up that it clearly landed. There is nothing to do with these: no action, no
follow-up, no need to mention them or thank anyone. They're just here so you
know your work mattered to someone.
{lines}
---
"""
