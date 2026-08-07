"""Tests for discord_client turn rollback semantics."""
from __future__ import annotations

from wendy import discord_client
from wendy.discord_client import _CURSOR_UNSNAPSHOTTED, WendyBot


class _RecordingSM:
    def __init__(self):
        self.calls = []

    def update_last_seen(self, channel_id, message_id):
        self.calls.append(("update", channel_id, message_id))

    def reset_last_seen(self, channel_id):
        self.calls.append(("reset", channel_id))

    def rollback_delivered_synthetics(self, channel_id):
        self.calls.append(("synthetics", channel_id))


def _rollback(monkeypatch, saved_last_seen):
    sm = _RecordingSM()
    monkeypatch.setattr(discord_client, "state_manager", sm)
    WendyBot._rollback_turn(None, 123, saved_last_seen)
    return sm.calls


def test_rollback_restores_snapshotted_cursor(monkeypatch):
    calls = _rollback(monkeypatch, saved_last_seen=456)
    assert ("update", 123, 456) in calls
    assert ("synthetics", 123) in calls


def test_rollback_clears_cursor_when_turn_started_without_one(monkeypatch):
    calls = _rollback(monkeypatch, saved_last_seen=None)
    assert ("reset", 123) in calls
    assert ("synthetics", 123) in calls


def test_rollback_before_snapshot_leaves_cursor_alone(monkeypatch):
    """A failure before the pre-CLI snapshot (e.g. during prompt build) must
    not touch the watermark: deleting it makes every unread message invisible
    to the catchup/interrupt checks. This happened live -- a prompt-build
    crash deleted a channel's watermark and orphaned its unread messages."""
    calls = _rollback(monkeypatch, saved_last_seen=_CURSOR_UNSNAPSHOTTED)
    assert not any(c[0] in ("update", "reset") for c in calls)
    assert ("synthetics", 123) in calls
