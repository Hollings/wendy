"""Tests for forwarded-message flattening (wendy/forwards.py) and its client wiring."""
from __future__ import annotations

import asyncio
import datetime
import types

import pytest

from wendy import discord_client, forwards
from wendy.discord_client import WendyBot
from wendy.forwards import (
    FORWARD_FOOTER,
    FORWARD_HEADER,
    all_attachments,
    has_visible_payload,
    is_forward,
    render_forwarded_content,
)


def _att(filename: str, data: bytes = b"x"):
    async def read():
        return data
    return types.SimpleNamespace(filename=filename, url=f"https://cdn/{filename}", read=read)


def _snap(content="", attachments=(), embeds=(), stickers=(), created_at=None):
    return types.SimpleNamespace(
        content=content, attachments=list(attachments), embeds=list(embeds),
        stickers=list(stickers), created_at=created_at,
    )


def _msg(content="", attachments=(), snapshots=None, mentions=()):
    return types.SimpleNamespace(
        content=content, attachments=list(attachments), message_snapshots=snapshots,
        mentions=list(mentions), id=555, reference=None, webhook_id=None,
        channel=types.SimpleNamespace(id=42),
        guild=types.SimpleNamespace(id=7),
        author=types.SimpleNamespace(id=9, display_name="john", bot=False),
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_plain_message_is_untouched():
    m = _msg(content="hi")
    assert not is_forward(m)
    assert render_forwarded_content(m, "hi") == "hi"
    assert has_visible_payload(m)


def test_snapshots_attr_missing_is_tolerated():
    """Older discord.py builds have no ``message_snapshots`` attribute at all."""
    m = types.SimpleNamespace(content="", attachments=[])
    assert not is_forward(m)
    assert not has_visible_payload(m)
    assert all_attachments(m) == []


def test_empty_message_without_forward_has_no_payload():
    assert not has_visible_payload(_msg(content="   "))


def test_forward_with_text_only_has_payload():
    m = _msg(snapshots=[_snap(content="the forwarded thing")])
    assert is_forward(m)
    assert has_visible_payload(m)


def test_forward_with_attachment_only_has_payload():
    m = _msg(snapshots=[_snap(attachments=[_att("a.png")])])
    assert has_visible_payload(m)


def test_render_wraps_snapshot_text_in_markers():
    m = _msg(snapshots=[_snap(content="quoted text")])
    out = render_forwarded_content(m, "")
    assert out.startswith(FORWARD_HEADER)
    assert "quoted text" in out
    assert out.endswith(FORWARD_FOOTER)


def test_render_keeps_forwarder_text_before_forward():
    m = _msg(content="look at this", snapshots=[_snap(content="quoted")])
    out = render_forwarded_content(m, "look at this")
    assert out.index("look at this") < out.index(FORWARD_HEADER)
    assert out.index(FORWARD_HEADER) < out.index("quoted")


def test_render_includes_original_timestamp():
    when = datetime.datetime(2025, 3, 4, 5, 6, tzinfo=datetime.UTC)
    m = _msg(snapshots=[_snap(content="x", created_at=when)])
    assert "originally sent 2025-03-04 05:06 UTC" in render_forwarded_content(m, "")


def test_render_flattens_embeds():
    embed = types.SimpleNamespace(
        title="Cool page", description="A description", url="https://example.com",
        fields=[types.SimpleNamespace(name="k", value="v")],
    )
    m = _msg(snapshots=[_snap(embeds=[embed])])
    out = render_forwarded_content(m, "")
    assert "embed: Cool page | A description | https://example.com | k: v" in out


def test_render_names_attachment_only_forward():
    m = _msg(snapshots=[_snap(attachments=[_att("photo.jpg"), _att("doc.pdf")])])
    out = render_forwarded_content(m, "")
    assert "(attachments only: photo.jpg, doc.pdf)" in out


def test_render_marks_truly_empty_snapshot():
    m = _msg(snapshots=[_snap()])
    assert "(empty)" in render_forwarded_content(m, "")


def test_render_lists_stickers():
    m = _msg(snapshots=[_snap(stickers=[types.SimpleNamespace(name="wave")])])
    assert "stickers: wave" in render_forwarded_content(m, "")


def test_render_multiple_snapshots_each_delimited():
    m = _msg(snapshots=[_snap(content="one"), _snap(content="two")])
    out = render_forwarded_content(m, "")
    assert out.count(FORWARD_HEADER) == 2
    assert out.count(FORWARD_FOOTER) == 2


def test_all_attachments_orders_own_before_forwarded():
    own = _att("own.png")
    fwd = _att("fwd.png")
    m = _msg(attachments=[own], snapshots=[_snap(attachments=[fwd])])
    assert all_attachments(m) == [own, fwd]


# ---------------------------------------------------------------------------
# Client wiring
# ---------------------------------------------------------------------------

class _RecordingSM:
    def __init__(self):
        self.inserted = []

    def insert_message(self, **kw):
        self.inserted.append(kw)


def _bot():
    # _resolve_mentions is a staticmethod, so it binds cleanly onto a stand-in.
    return types.SimpleNamespace(_resolve_mentions=WendyBot._resolve_mentions)


def test_cache_message_stores_forwarded_text(monkeypatch):
    sm = _RecordingSM()
    monkeypatch.setattr(discord_client, "state_manager", sm)
    m = _msg(content="fyi", snapshots=[_snap(content="forwarded body")])
    WendyBot._cache_message(_bot(), m)
    assert len(sm.inserted) == 1
    content = sm.inserted[0]["content"]
    assert content.startswith("fyi\n" + FORWARD_HEADER)
    assert "forwarded body" in content
    assert sm.inserted[0]["attachment_urls"] is None


def test_cache_message_leaves_plain_content_alone(monkeypatch):
    sm = _RecordingSM()
    monkeypatch.setattr(discord_client, "state_manager", sm)
    WendyBot._cache_message(_bot(), _msg(content="plain"))
    assert sm.inserted[0]["content"] == "plain"


def test_save_attachments_downloads_forwarded_files(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_client, "attachments_dir", lambda name: tmp_path / name)
    m = _msg(
        attachments=[_att("own.png", b"own")],
        snapshots=[_snap(attachments=[_att("fwd.txt", b"fwd")])],
    )
    saved = asyncio.run(WendyBot._save_attachments(types.SimpleNamespace(), m, "chan"))
    names = sorted(p.name for p in (tmp_path / "chan").iterdir())
    assert names == ["msg_555_0_own.png", "msg_555_1_fwd.txt"]
    assert (tmp_path / "chan" / "msg_555_1_fwd.txt").read_bytes() == b"fwd"
    assert len(saved) == 2


def test_save_attachments_noop_without_files(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_client, "attachments_dir", lambda name: tmp_path / name)
    saved = asyncio.run(WendyBot._save_attachments(types.SimpleNamespace(), _msg(content="hi"), "chan"))
    assert saved == []
    assert not (tmp_path / "chan").exists()


@pytest.mark.parametrize("snapshots", [None, []])
def test_forwards_module_tolerates_empty_snapshot_lists(snapshots):
    m = _msg(content="", snapshots=snapshots)
    assert not forwards.is_forward(m)


# ---------------------------------------------------------------------------
# Against the real discord.py Message class (guards the attribute name)
# ---------------------------------------------------------------------------

def _real_forward_message():
    from unittest.mock import MagicMock

    import discord

    state = MagicMock()
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 42
    data = {
        "id": "555", "channel_id": "42", "content": "", "tts": False,
        "mention_everyone": False, "attachments": [], "embeds": [],
        "edited_timestamp": None, "type": 0, "pinned": False, "mentions": [],
        "mention_roles": [], "timestamp": "2026-01-01T00:00:00+00:00",
        "author": {"id": "9", "username": "john", "discriminator": "0",
                   "avatar": None, "global_name": "john"},
        "flags": 16384,
        "message_reference": {"type": 1, "message_id": "1", "channel_id": "2", "guild_id": "3"},
        "message_snapshots": [{"message": {
            "content": "forwarded body",
            "timestamp": "2025-03-04T05:06:00+00:00", "edited_timestamp": None,
            "type": 0, "flags": 0, "mentions": [], "mention_roles": [], "embeds": [],
            "attachments": [{"id": "77", "filename": "pic.png", "size": 3,
                             "url": "https://cdn/pic.png", "proxy_url": "https://cdn/pic.png"}],
        }}],
    }
    return discord.Message(state=state, channel=channel, data=data)


def test_real_discord_message_forward_is_detected():
    m = _real_forward_message()
    assert m.content == ""
    assert is_forward(m)
    assert has_visible_payload(m)


def test_real_discord_message_forward_renders_text_and_files():
    m = _real_forward_message()
    out = render_forwarded_content(m, m.content)
    assert "forwarded body" in out
    assert "originally sent 2025-03-04 05:06 UTC" in out
    assert [a.filename for a in all_attachments(m)] == ["pic.png"]
