"""Forwarded-message support.

A Discord *forward* arrives as a message with empty ``content`` and a
``reference`` of type ``forward``. The forwarded text, embeds and attachments
live in ``message.snapshots`` (``discord.MessageSnapshot``), not on the message
itself. This module flattens those snapshots so the rest of the bot can treat a
forward like an ordinary message: text goes into the ``content`` column and
snapshot attachments are downloaded alongside the message's own.

No internal imports -- this is a leaf module.
"""
from __future__ import annotations

import datetime
from typing import Any

FORWARD_HEADER = "[Forwarded message]"
FORWARD_FOOTER = "[End of forwarded message]"


def is_forward(message: Any) -> bool:
    """Return True if *message* carries at least one forwarded snapshot."""
    return bool(_snapshots(message))


def _snapshots(message: Any) -> list[Any]:
    # ``snapshots`` was added in discord.py 2.5; older builds simply lack it.
    return list(getattr(message, "snapshots", None) or [])


def snapshot_attachments(message: Any) -> list[Any]:
    """Return every attachment carried by the message's forwarded snapshots."""
    found: list[Any] = []
    for snap in _snapshots(message):
        found.extend(getattr(snap, "attachments", None) or [])
    return found


def all_attachments(message: Any) -> list[Any]:
    """The message's own attachments followed by any forwarded ones.

    Ordering matters: saved files are named ``msg_{id}_{index}_{name}`` and
    surfaced to Wendy sorted by that pattern, so forwarded files land after
    the message's own.
    """
    return list(getattr(message, "attachments", None) or []) + snapshot_attachments(message)


def has_visible_payload(message: Any) -> bool:
    """True if the message has anything Wendy could read: text, files, or a forward."""
    content = getattr(message, "content", "") or ""
    if content.strip():
        return True
    if getattr(message, "attachments", None):
        return True
    return is_forward(message)


def render_forwarded_content(message: Any, own_content: str) -> str:
    """Combine a message's own (already mention-resolved) text with its forwards.

    Each snapshot is rendered as a delimited block so Wendy can tell the
    forwarded text apart from anything the forwarder typed. Discord does not
    include the original author in a snapshot, so none is claimed here.
    """
    snapshots = _snapshots(message)
    if not snapshots:
        return own_content

    blocks: list[str] = []
    own = (own_content or "").strip()
    if own:
        blocks.append(own)

    for snap in snapshots:
        blocks.append(_render_snapshot(snap))

    return "\n".join(blocks)


def _render_snapshot(snap: Any) -> str:
    lines = [_header_for(snap)]

    text = (getattr(snap, "content", "") or "").strip()
    if text:
        lines.append(text)

    embed_lines = _render_embeds(getattr(snap, "embeds", None) or [])
    if embed_lines:
        lines.extend(embed_lines)

    sticker_names = [
        getattr(s, "name", None) for s in (getattr(snap, "stickers", None) or [])
    ]
    sticker_names = [n for n in sticker_names if n]
    if sticker_names:
        lines.append("stickers: " + ", ".join(sticker_names))

    attachments = getattr(snap, "attachments", None) or []
    if attachments and not text and not embed_lines:
        names = [getattr(a, "filename", None) or "file" for a in attachments]
        lines.append("(attachments only: " + ", ".join(names) + ")")

    if len(lines) == 1:
        lines.append("(empty)")

    lines.append(FORWARD_FOOTER)
    return "\n".join(lines)


def _header_for(snap: Any) -> str:
    created = getattr(snap, "created_at", None)
    if isinstance(created, datetime.datetime):
        stamp = created.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
        return f"{FORWARD_HEADER} (originally sent {stamp})"
    return FORWARD_HEADER


def _render_embeds(embeds: list[Any]) -> list[str]:
    """Flatten embeds into readable lines. Link previews are the common case."""
    lines: list[str] = []
    for embed in embeds:
        parts: list[str] = []
        title = getattr(embed, "title", None)
        description = getattr(embed, "description", None)
        url = getattr(embed, "url", None)
        if title:
            parts.append(str(title))
        if description:
            parts.append(str(description))
        if url and url not in parts:
            parts.append(str(url))
        for field in getattr(embed, "fields", None) or []:
            name = getattr(field, "name", None)
            value = getattr(field, "value", None)
            if name or value:
                parts.append(f"{name or ''}: {value or ''}".strip(": "))
        if parts:
            lines.append("embed: " + " | ".join(parts))
    return lines
