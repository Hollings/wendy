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
