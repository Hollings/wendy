"""Tests for discord_client turn rollback semantics."""
from __future__ import annotations

import types

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


# ---------------------------------------------------------------------------
# Generation start / WENDY interrupt orchestration
# ---------------------------------------------------------------------------

class _FakeLoop:
    def __init__(self):
        self.created = []

    def create_task(self, coro):
        self.created.append(coro)
        return types.SimpleNamespace(done=lambda: False, cancel=lambda: None)


def _fake_bot():
    bot = types.SimpleNamespace(
        _paused=False, loop=_FakeLoop(), _active_generations={}, channel_configs={}, synthetics=[],
    )
    # Plain callables (not coroutines) so nothing is left un-awaited.
    bot._generate_response = lambda channel, job, model_override=None: ("gen", channel, job, model_override)
    bot._insert_synthetic_message = lambda channel_id, author, content: bot.synthetics.append(
        (channel_id, author, content))
    return bot


def test_start_generation_leaves_model_override_to_run_cli():
    """The channel's configured model must not travel as ``model_override``.

    ``resolve_model`` only honours WENDY_MODEL_OVERRIDE when model_override is
    None, so passing the channel model here exempted every channel with an
    explicit model: with the override set to opus, coding (model=sonnet) kept
    running Sonnet while chat (no model) switched. This happened live."""
    bot = _fake_bot()
    channel = types.SimpleNamespace(id=42)
    WendyBot._start_generation(bot, channel, {"model": "sonnet"})
    job = bot._active_generations[42]
    assert job.task is not None
    assert bot.loop.created == [("gen", channel, job, None)]


def test_start_generation_skips_when_paused():
    bot = _fake_bot()
    bot._paused = True
    WendyBot._start_generation(bot, types.SimpleNamespace(id=42), {"model": "sonnet"})
    assert bot.loop.created == [] and bot._active_generations == {}


def test_interrupt_swaps_job_before_cancel_and_restarts_without_override():
    """WENDY: the active-job entry is replaced *before* cancel() so the old
    task's finally sees a foreign job and won't restart itself; a synthetic
    system message is queued; the fresh turn carries no model override."""
    bot = _fake_bot()
    seen_at_cancel = []
    old_job = discord_client.GenerationJob()
    old_job.task = types.SimpleNamespace(
        done=lambda: False,
        cancel=lambda: seen_at_cancel.append(bot._active_generations[42]),
    )
    bot._active_generations[42] = old_job
    message = types.SimpleNamespace(
        channel=types.SimpleNamespace(id=42),
        author=types.SimpleNamespace(display_name="delta"),
    )

    WendyBot._interrupt_channel(bot, message, old_job)

    new_job = bot._active_generations[42]
    assert new_job is not old_job
    assert seen_at_cancel == [new_job]
    assert bot.synthetics == [
        (42, "System", "[delta interrupted you. Whatever you were doing may not be finished.]"),
    ]
    assert bot.loop.created == [("gen", message.channel, new_job, None)]
    assert new_job.task is not None


def test_finalize_ignores_a_job_that_was_interrupted():
    """The cancelled task's finally must not touch the replacement job."""
    bot = _fake_bot()
    channel = types.SimpleNamespace(id=42)
    old_job, new_job = discord_client.GenerationJob(), discord_client.GenerationJob()
    old_job.new_message_pending = True  # would normally trigger a restart
    bot._active_generations[42] = new_job
    WendyBot._finalize_generation(bot, channel, old_job)
    assert bot._active_generations[42] is new_job
    assert bot.loop.created == []
