"""Tests for discord_client turn rollback semantics."""
from __future__ import annotations

import types

from wendy import discord_client
from wendy.cli import is_transient_api_failure
from wendy.discord_client import _CURSOR_UNSNAPSHOTTED, WendyBot
from wendy.recovery import OutageRecovery


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

class _Timer:
    def __init__(self, loop, delay, cb, args):
        self.loop, self.delay, self.cb, self.args = loop, delay, cb, args

    def cancel(self):
        self.loop.timers.remove(self)

    def fire(self):
        self.loop.timers.remove(self)
        self.cb(*self.args)


class _FakeLoop:
    def __init__(self):
        self.created = []
        self.timers: list[_Timer] = []

    def create_task(self, coro):
        self.created.append(coro)
        return types.SimpleNamespace(done=lambda: False, cancel=lambda: None)

    def call_later(self, delay, cb, *args):
        timer = _Timer(self, delay, cb, args)
        self.timers.append(timer)
        return timer


def _fake_bot(pending=True):
    bot = types.SimpleNamespace(
        _paused=False, loop=_FakeLoop(), _active_generations={}, channel_configs={}, synthetics=[],
        notices=[], channels={},
    )
    bot._recovery = OutageRecovery(lambda: bot.loop, base_delay=120, max_delay=1800)
    # Plain callables (not coroutines) so nothing is left un-awaited.
    bot._generate_response = lambda channel, job, model_override=None: ("gen", channel, job, model_override)
    bot._insert_synthetic_message = lambda channel_id, author, content: bot.synthetics.append(
        (channel_id, author, content))
    bot._has_pending_messages = lambda channel_id: pending
    bot._job_is_running = WendyBot._job_is_running
    bot._start_generation = lambda channel, cfg: WendyBot._start_generation(bot, channel, cfg)
    bot._schedule_recovery = lambda channel: WendyBot._schedule_recovery(bot, channel)
    bot._fire_recovery = lambda channel_id: WendyBot._fire_recovery(bot, channel_id)
    # Returns a marker that lands in loop.created, standing in for the coroutine.
    bot._send_outage_notice = lambda channel, delay: ("notice", channel.id, delay)
    bot.get_channel = lambda channel_id: bot.channels.get(channel_id)
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


# ---------------------------------------------------------------------------
# Outage recovery: retry the queue after the Claude API errors out
# ---------------------------------------------------------------------------

def _channel(bot, channel_id=42):
    channel = types.SimpleNamespace(id=channel_id)
    bot.channels[channel_id] = channel
    return channel


def test_transient_api_failure_classification():
    """500s and other 5xx must retry like 529s. Live: a turn died on a 500
    minutes before the 529s started and was treated as a hard failure."""
    assert is_transient_api_failure("API Error: 529 Overloaded. This is a server-side issue")
    assert is_transient_api_failure("API Error: 500 Internal server error. This is a server-side issue")
    assert is_transient_api_failure("API Error: 503 Service Unavailable")
    assert is_transient_api_failure("CLI succeeded but API returned overloaded_error")
    assert not is_transient_api_failure("API Error: 401 Unauthorized")
    assert not is_transient_api_failure("API Error: 400 invalid_request_error")
    assert not is_transient_api_failure("OAuth token expired")
    assert not is_transient_api_failure("")
    assert not is_transient_api_failure(None)


def test_finalize_schedules_recovery_when_ladder_gave_up():
    """recovery_needed arms a backoff timer, drops the active job, posts the
    outage notice once, and does NOT start an immediate turn even though
    messages arrived mid-turn (the API is down; it would fail again)."""
    bot = _fake_bot(pending=True)
    channel = _channel(bot)
    job = discord_client.GenerationJob()
    job.recovery_needed = True
    job.new_message_pending = True
    bot._active_generations[42] = job

    WendyBot._finalize_generation(bot, channel, job)

    assert 42 not in bot._active_generations
    assert bot._recovery.pending(42)
    assert bot._recovery.attempts(42) == 1
    assert [t.delay for t in bot.loop.timers] == [120]
    assert bot.loop.created == [("notice", 42, 120)]


def test_recovery_notice_only_on_first_attempt_of_an_outage():
    bot = _fake_bot(pending=True)
    channel = _channel(bot)
    for _ in range(3):
        job = discord_client.GenerationJob()
        job.recovery_needed = True
        bot._active_generations[42] = job
        WendyBot._finalize_generation(bot, channel, job)
    notices = [c for c in bot.loop.created if c[0] == "notice"]
    assert notices == [("notice", 42, 120)]
    assert bot._recovery.attempts(42) == 3
    assert [t.delay for t in bot.loop.timers] == [480]  # 120 -> 240 -> 480, one armed


def test_recovery_skipped_when_queue_is_empty():
    bot = _fake_bot(pending=False)
    channel = _channel(bot)
    job = discord_client.GenerationJob()
    job.recovery_needed = True
    bot._active_generations[42] = job
    WendyBot._finalize_generation(bot, channel, job)
    assert not bot._recovery.pending(42)
    assert bot.loop.created == []


def test_recovery_fire_starts_a_fresh_turn_and_keeps_backoff():
    bot = _fake_bot(pending=True)
    channel = _channel(bot)
    bot._recovery.schedule(42, bot._fire_recovery)

    bot.loop.timers[0].fire()

    job = bot._active_generations[42]
    assert bot.loop.created == [("gen", channel, job, None)]
    assert not bot._recovery.pending(42)
    # Attempts persist until a turn succeeds, so another failure backs off further.
    assert bot._recovery.attempts(42) == 1
    assert bot._recovery.delay_for(42) == 240


def test_recovery_fire_stands_down_when_a_turn_is_already_running():
    bot = _fake_bot(pending=True)
    _channel(bot)
    running = discord_client.GenerationJob()
    running.task = types.SimpleNamespace(done=lambda: False, cancel=lambda: None)
    bot._active_generations[42] = running
    bot._recovery.schedule(42, bot._fire_recovery)

    bot.loop.timers[0].fire()

    assert bot._active_generations[42] is running
    assert bot.loop.created == []


def test_recovery_fire_resets_when_queue_drained_or_paused():
    bot = _fake_bot(pending=False)
    _channel(bot)
    bot._recovery.schedule(42, bot._fire_recovery)
    bot.loop.timers[0].fire()
    assert bot._recovery.attempts(42) == 0 and bot.loop.created == []

    bot = _fake_bot(pending=True)
    _channel(bot)
    bot._paused = True
    bot._recovery.schedule(42, bot._fire_recovery)
    bot.loop.timers[0].fire()
    assert bot.loop.created == []


def test_new_turn_cancels_pending_recovery_but_keeps_attempts():
    """A human message during the backoff starts a turn that reads the queue,
    so the timer is redundant. The counter survives: if that turn fails too,
    the next retry keeps backing off instead of restarting at the base delay."""
    bot = _fake_bot(pending=True)
    channel = _channel(bot)
    bot._recovery.schedule(42, bot._fire_recovery)
    bot._recovery.schedule(42, bot._fire_recovery)
    assert bot._recovery.pending(42)

    WendyBot._start_generation(bot, channel, {})

    assert not bot._recovery.pending(42)
    assert bot.loop.timers == []
    assert bot._recovery.attempts(42) == 2
    assert 42 in bot._active_generations
