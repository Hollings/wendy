"""Tests for wendy.api_server pure request-validation helpers.

These cover the security-sensitive input validation that guards the internal
API: attachment-path containment (symlink/traversal escape), channel-id
parsing, and the shared ``channel.send()`` kwargs builder (message-length cap,
attachment validation, reply references).
"""
from __future__ import annotations

import os

from wendy.api_server import (
    _build_discord_send_kwargs,
    _parse_channel_id,
    _validate_attachment_path,
)
from wendy.config import DISCORD_MAX_MESSAGE_LENGTH

# =========================================================================
# _validate_attachment_path -- containment / symlink escape guard
# =========================================================================


def test_validate_attachment_path_accepts_existing_tmp_file(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("hi")
    # /tmp is an allowed parent; tmp_path lives under it on Linux CI.
    real = "/tmp/_wendy_att_ok.txt"
    try:
        with open(real, "w") as fh:
            fh.write("hi")
        assert _validate_attachment_path(real) is None
    finally:
        if os.path.exists(real):
            os.remove(real)


def test_validate_attachment_path_rejects_outside_allowed_dirs():
    err = _validate_attachment_path("/etc/passwd")
    assert err is not None
    assert "must be in" in err


def test_validate_attachment_path_rejects_missing_file():
    err = _validate_attachment_path("/tmp/_wendy_does_not_exist_zzz")
    assert err is not None
    assert "not found" in err


def test_validate_attachment_path_rejects_symlink_escape():
    """A symlink inside an allowed dir that points outside must be rejected.

    ``resolve()`` expands the link to its real target before the
    allowed-parent check, so the escape is caught.
    """
    link = "/tmp/_wendy_escape_link"
    if os.path.lexists(link):
        os.remove(link)
    os.symlink("/etc/hostname", link)
    try:
        err = _validate_attachment_path(link)
        assert err is not None
        assert "must be in" in err
    finally:
        os.remove(link)


# =========================================================================
# _parse_channel_id
# =========================================================================


def test_parse_channel_id_valid():
    channel_id, err = _parse_channel_id({"channel_id": "123"})
    assert channel_id == 123
    assert err is None


def test_parse_channel_id_missing():
    channel_id, err = _parse_channel_id({})
    assert channel_id is None
    assert err is not None
    assert err.status == 400


def test_parse_channel_id_non_numeric():
    channel_id, err = _parse_channel_id({"channel_id": "abc"})
    assert channel_id is None
    assert err is not None
    assert err.status == 400


# =========================================================================
# _build_discord_send_kwargs
# =========================================================================


def test_build_send_kwargs_plain_content():
    kwargs, err = _build_discord_send_kwargs({"content": "hello"}, 42)
    assert err is None
    assert kwargs == {"content": "hello"}


def test_build_send_kwargs_message_alias():
    # ``message`` is accepted as an alias for ``content``.
    kwargs, err = _build_discord_send_kwargs({"message": "hi"}, 42)
    assert err is None
    assert kwargs["content"] == "hi"


def test_build_send_kwargs_empty_content_is_none():
    kwargs, err = _build_discord_send_kwargs({}, 42)
    assert err is None
    assert kwargs["content"] is None


def test_build_send_kwargs_rejects_too_long():
    kwargs, err = _build_discord_send_kwargs(
        {"content": "a" * (DISCORD_MAX_MESSAGE_LENGTH + 1)}, 42,
    )
    assert kwargs == {}
    assert err is not None
    assert "too long" in err.lower()


def test_build_send_kwargs_rejects_bad_attachment():
    kwargs, err = _build_discord_send_kwargs(
        {"content": "hi", "file_path": "/etc/shadow"}, 42,
    )
    assert kwargs == {}
    assert err is not None
    assert "must be in" in err


def test_build_send_kwargs_reply_reference():
    kwargs, err = _build_discord_send_kwargs(
        {"content": "hi", "reply_to": "999"}, 7,
    )
    assert err is None
    ref = kwargs.get("reference")
    assert ref is not None
    assert ref.message_id == 999
    assert ref.channel_id == 7


def test_build_send_kwargs_valid_attachment(tmp_path):
    real = "/tmp/_wendy_att_valid.txt"
    try:
        with open(real, "w") as fh:
            fh.write("data")
        kwargs, err = _build_discord_send_kwargs(
            {"content": "see file", "file_path": real}, 1,
        )
        assert err is None
        assert "file" in kwargs
    finally:
        if os.path.exists(real):
            os.remove(real)


def test_build_send_kwargs_multiple_attachments():
    a, b = "/tmp/_wendy_att_multi_a.txt", "/tmp/_wendy_att_multi_b.txt"
    try:
        for p in (a, b):
            with open(p, "w") as fh:
                fh.write("data")
        kwargs, err = _build_discord_send_kwargs(
            {"content": "see files", "attachments": [a, b]}, 1,
        )
        assert err is None
        assert "file" not in kwargs
        assert "files" in kwargs
        assert len(kwargs["files"]) == 2
    finally:
        for p in (a, b):
            if os.path.exists(p):
                os.remove(p)


def test_build_send_kwargs_single_item_attachments_list_uses_singular_key():
    real = "/tmp/_wendy_att_single_list.txt"
    try:
        with open(real, "w") as fh:
            fh.write("data")
        kwargs, err = _build_discord_send_kwargs(
            {"content": "see file", "attachments": [real]}, 1,
        )
        assert err is None
        assert "file" in kwargs
        assert "files" not in kwargs
    finally:
        if os.path.exists(real):
            os.remove(real)


def test_build_send_kwargs_rejects_bad_attachment_in_list():
    real = "/tmp/_wendy_att_valid_in_bad_list.txt"
    try:
        with open(real, "w") as fh:
            fh.write("data")
        kwargs, err = _build_discord_send_kwargs(
            {"content": "hi", "attachments": [real, "/etc/shadow"]}, 1,
        )
        assert kwargs == {}
        assert err is not None
        assert "must be in" in err
    finally:
        if os.path.exists(real):
            os.remove(real)


# =========================================================================
# _consume_delivered_messages -- the send-block retry loop fix
# =========================================================================


def test_consume_delivered_messages_unblocks_retry(tmp_path, monkeypatch):
    """A blocked send delivers the new messages in its response; consuming them
    must advance the watermark so the amended retry is not blocked again."""
    from wendy import api_server
    from wendy.config import SYNTHETIC_ID_THRESHOLD
    from wendy.state import StateManager

    sm = StateManager(db_path=tmp_path / "t.db")
    sm._get_conn()
    monkeypatch.setattr(api_server, "state_manager", sm)

    channel = 7
    bot_id = 999
    synth_id = SYNTHETIC_ID_THRESHOLD + 1

    def insert(mid, author_id=42, is_bot=False):
        sm.insert_message(
            message_id=mid, channel_id=channel, guild_id=1, author_id=author_id,
            author_nickname="user", is_bot=is_bot, content="hello", timestamp=1,
        )

    insert(2000)
    sm.update_last_seen(channel, 2000)  # watermark exists (mid-turn)
    insert(2001)
    insert(2002)
    insert(synth_id, author_id=0)

    # These messages arrive in the blocked-send response...
    pending = sm.check_for_new_messages(
        channel, bot_user_id=bot_id,
        synthetic_id_threshold=SYNTHETIC_ID_THRESHOLD, max_limit=50,
    )
    assert {m["message_id"] for m in pending} == {2001, 2002, synth_id}

    api_server._consume_delivered_messages(channel, pending)

    # ...so the retry sees nothing new and goes through. The delivered
    # synthetic must not re-block either (it used to re-block every wake turn).
    assert sm.get_last_seen(channel) == 2002
    retry_pending = sm.check_for_new_messages(
        channel, bot_user_id=bot_id,
        synthetic_id_threshold=SYNTHETIC_ID_THRESHOLD, max_limit=50,
    )
    assert retry_pending == []
    # The synthetic is marked delivered, not deleted (commit/rollback later).
    rows = sm.fetch_messages(channel, since_id=2002)
    assert rows == []


def test_check_for_new_messages_includes_attachments(tmp_path, monkeypatch):
    """The blocked-send payload is the ONLY delivery of the messages it
    carries, so an image-only message must include its attachment paths --
    otherwise Wendy sees empty content and has no way to find the file."""
    from wendy import api_server
    from wendy.state import StateManager

    sm = StateManager(db_path=tmp_path / "t.db")
    sm._get_conn()
    monkeypatch.setattr(api_server, "state_manager", sm)
    monkeypatch.setattr(api_server, "get_channel_name", lambda cid: "chat")

    att = ["/data/wendy/channels/chat/attachments/msg_3001_0_cat.png"]
    monkeypatch.setattr(
        api_server, "find_attachments_for_message",
        lambda mid, name: att if mid == 3001 else [],
    )

    channel = 7
    sm.insert_message(
        message_id=3000, channel_id=channel, guild_id=1, author_id=42,
        author_nickname="user", is_bot=False, content="hi", timestamp=1,
    )
    sm.update_last_seen(channel, 3000)
    sm.insert_message(
        message_id=3001, channel_id=channel, guild_id=1, author_id=42,
        author_nickname="user", is_bot=False, content="", timestamp=2,
    )

    pending = api_server.check_for_new_messages(channel)
    assert [m["message_id"] for m in pending] == [3001]
    assert pending[0]["attachments"] == att
