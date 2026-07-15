"""Tests for the bin/ CLI helper tools (msg, msgs, react, wake).

These scripts have no .py extension and are normally run standalone from PATH,
so they're loaded here via importlib. Importing them only executes module-level
definitions -- main() runs solely under ``if __name__ == "__main__"`` -- so no
network calls are made.
"""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).parent.parent / "bin"


def _load(name):
    path = BIN_DIR / name
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


msg = _load("msg")
msgs = _load("msgs")
react = _load("react")
wake = _load("wake")


# --------------------------------------------------------------------------- #
# bin/wake -- duration / timestamp parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", 30),
        ("30sec", 30),
        ("10seconds", 10),
        ("5m", 300),
        ("5min", 300),
        ("15 min", 900),
        ("2minutes", 120),
        ("1h", 3600),
        ("1hr", 3600),
        ("2h", 7200),
        ("1hour", 3600),
        ("3hours", 10800),
        ("  45m  ", 2700),
        ("2H", 7200),
    ],
)
def test_parse_duration_valid(text, expected):
    assert wake.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "5d", "5", "m", "1.5h", "-5m", "five minutes"])
def test_parse_duration_invalid(text):
    assert wake.parse_duration(text) is None


def test_parse_timestamp_future_iso_is_positive():
    delay = wake.parse_timestamp("2030-01-01T00:00")
    assert delay is not None and delay > 0


def test_parse_timestamp_past_iso_is_none():
    assert wake.parse_timestamp("2020-01-01T00:00") is None


def test_parse_timestamp_bare_time_wraps_within_a_day():
    # A bare HH:MM in the past is interpreted as tomorrow, so the delay is
    # always positive and never more than 24h away.
    delay = wake.parse_timestamp("00:00")
    assert delay is not None
    assert 0 < delay <= 86400


@pytest.mark.parametrize("text", ["nope", "25:00", "2026-13-01T00:00", ""])
def test_parse_timestamp_invalid(text):
    assert wake.parse_timestamp(text) is None


# --------------------------------------------------------------------------- #
# bin/react -- emoji resolution
# --------------------------------------------------------------------------- #

def test_resolve_emoji_known_name():
    assert react.resolve_emoji("fire") == "\U0001f525"
    assert react.resolve_emoji("thumbsup") == "\U0001f44d"


def test_resolve_emoji_unknown_passthrough():
    # Raw unicode and custom server emoji must survive unchanged.
    assert react.resolve_emoji("\U0001f600") == "\U0001f600"
    assert react.resolve_emoji(":wendy:") == ":wendy:"


def test_emoji_map_aliases_agree():
    # Documented aliases should map to the same glyph.
    assert react.EMOJI_MAP["joy"] == react.EMOJI_MAP["laugh"]
    assert react.EMOJI_MAP["sob"] == react.EMOJI_MAP["crying"]
    assert react.EMOJI_MAP["tada"] == react.EMOJI_MAP["party"]


# --------------------------------------------------------------------------- #
# bin/msg -- request body assembly
# --------------------------------------------------------------------------- #

def test_build_body_minimal():
    assert msg.build_body("123", "hello") == {"channel_id": "123", "content": "hello"}


def test_build_body_omits_unset_fields():
    body = msg.build_body("123", "hi", file=None, reply=None, force=False)
    assert "attachment" not in body
    assert "reply_to" not in body
    assert "force" not in body


def test_build_body_full():
    body = msg.build_body("123", "hi", file="/x.png", reply="999", force=True)
    assert body == {
        "channel_id": "123",
        "content": "hi",
        "attachment": "/x.png",
        "reply_to": "999",
        "force": True,
    }


# --------------------------------------------------------------------------- #
# bin/msg -- error rendering (blocked-send delivery)
# --------------------------------------------------------------------------- #

def test_format_error_output_plain_error():
    out = msg.format_error_output({"error": "Channel 5 not found"})
    assert out == "Error: Channel 5 not found"


def test_format_error_output_blocked_send_includes_new_messages():
    # A blocked send consumes the messages server-side; the error response is
    # their only delivery. The full payload must be shown or they are lost
    # (a follow-up `msgs` reports nothing new).
    result = {
        "error": "Send blocked: new messages arrived since your last check.",
        "new_messages": [{"message_id": 123, "author": "alice", "content": "hey wendy"}],
        "guidance": "Prefer sending ONE message that responds to all users at once.",
    }
    out = msg.format_error_output(result)
    assert "hey wendy" in out
    assert "alice" in out
    assert "guidance" in out


def test_format_error_output_guidance_without_messages_still_shown():
    result = {"error": "blocked", "new_messages": [], "guidance": "do the thing"}
    out = msg.format_error_output(result)
    assert "do the thing" in out


# --------------------------------------------------------------------------- #
# bin/msgs -- filtering and formatting
# --------------------------------------------------------------------------- #

def test_filter_messages_keeps_synthetic():
    # Synthetic system messages (task completions, self-wakes) are kept --
    # they are often the entire reason Wendy was woken.
    messages = [
        {"message_id": 1, "author": "alice", "author_id": "10"},
        {"message_id": msgs.SYNTHETIC_THRESHOLD, "author": "Task System", "author_id": "0"},
        {"message_id": msgs.SYNTHETIC_THRESHOLD + 5, "author": "Self-Wake", "author_id": "0"},
    ]
    out = msgs.filter_messages(messages, bot_id="42")
    assert [m["message_id"] for m in out] == [1, msgs.SYNTHETIC_THRESHOLD, msgs.SYNTHETIC_THRESHOLD + 5]


def test_filter_messages_strips_leading_bot_messages():
    messages = [
        {"message_id": 1, "author": "wendy", "author_id": "42"},
        {"message_id": 2, "author": "wendy", "author_id": "42"},
        {"message_id": 3, "author": "alice", "author_id": "10"},
    ]
    out = msgs.filter_messages(messages, bot_id="42")
    assert [m["message_id"] for m in out] == [3]


def test_filter_messages_keeps_bot_after_human():
    # A bot message that follows a human message is kept (it's context).
    messages = [
        {"message_id": 1, "author": "alice", "author_id": "10"},
        {"message_id": 2, "author": "wendy", "author_id": "42"},
    ]
    out = msgs.filter_messages(messages, bot_id="42")
    assert [m["message_id"] for m in out] == [1, 2]


def test_format_message_synthetic():
    line = msgs.format_message({
        "message_id": msgs.SYNTHETIC_THRESHOLD + 1,
        "author": "Task System",
        "content": "task done",
        "timestamp": 0,
    })
    assert "(system)" in line
    assert "task done" in line
    assert str(msgs.SYNTHETIC_THRESHOLD + 1) not in line


def test_format_message_basic():
    line = msgs.format_message({"message_id": 7, "author": "alice", "content": "hey", "timestamp": 0})
    assert "alice" in line
    assert "id:7" in line
    assert "hey" in line
    assert "??:??" in line  # no usable timestamp


def test_format_message_bad_timestamp_does_not_raise():
    # A non-numeric timestamp must degrade to "??:??" rather than crashing.
    line = msgs.format_message({"message_id": 7, "author": "alice", "content": "hi", "timestamp": "nope"})
    assert "??:??" in line


def test_format_message_with_reply_truncates_long_quote():
    long_quote = "x" * 200
    line = msgs.format_message({
        "message_id": 7,
        "author": "alice",
        "content": "reply body",
        "timestamp": 0,
        "reply_to": {"author": "bob", "content": long_quote},
    })
    assert "replying to bob" in line
    assert "..." in line
    assert "x" * 200 not in line


def test_format_message_with_attachments():
    line = msgs.format_message({
        "message_id": 7,
        "author": "alice",
        "content": "look",
        "timestamp": 0,
        "attachments": ["http://example.com/cat.png"],
    })
    assert "attachment:" in line
    assert "cat.png" in line
