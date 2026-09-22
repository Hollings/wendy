"""Tests for the outage recovery scheduler (wendy/recovery.py)."""
from __future__ import annotations

from wendy.recovery import OutageRecovery


class _Handle:
    def __init__(self, loop, delay, cb, args):
        self.loop, self.delay, self.cb, self.args = loop, delay, cb, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self.loop.handles.remove(self)

    def fire(self):
        self.loop.handles.remove(self)
        self.cb(*self.args)


class _FakeLoop:
    def __init__(self):
        self.handles: list[_Handle] = []

    def call_later(self, delay, cb, *args):
        handle = _Handle(self, delay, cb, args)
        self.handles.append(handle)
        return handle


def _recovery(base=120, cap=1800):
    loop = _FakeLoop()
    return loop, OutageRecovery(lambda: loop, base_delay=base, max_delay=cap)


def test_backoff_doubles_until_capped():
    loop, rec = _recovery(base=120, cap=1000)
    fired = []
    delays = [rec.schedule(7, fired.append) for _ in range(5)]
    assert delays == [120, 240, 480, 960, 1000]
    assert rec.attempts(7) == 5
    # Re-scheduling replaces the armed timer rather than stacking them.
    assert len(loop.handles) == 1
    assert rec.pending(7)


def test_fire_invokes_callback_and_clears_pending():
    loop, rec = _recovery()
    fired = []
    rec.schedule(7, fired.append)
    loop.handles[0].fire()
    assert fired == [7]
    assert not rec.pending(7)
    # Attempts persist across a fire so a failed retry backs off further.
    assert rec.attempts(7) == 1
    assert rec.delay_for(7) == 240


def test_cancel_keeps_attempt_counter_reset_clears_it():
    loop, rec = _recovery()
    rec.schedule(7, lambda _: None)
    assert rec.cancel(7) is True
    assert rec.cancel(7) is False
    assert not loop.handles
    assert rec.attempts(7) == 1

    rec.schedule(7, lambda _: None)
    rec.reset(7)
    assert not loop.handles
    assert rec.attempts(7) == 0
    assert rec.delay_for(7) == 120


def test_channels_are_independent():
    loop, rec = _recovery()
    rec.schedule(1, lambda _: None)
    rec.schedule(1, lambda _: None)
    rec.schedule(2, lambda _: None)
    assert rec.attempts(1) == 2 and rec.attempts(2) == 1
    assert len(loop.handles) == 2
    rec.cancel_all()
    assert not loop.handles
    assert rec.attempts(1) == 2  # cancel_all is shutdown, not success


def test_callback_exception_is_contained():
    loop, rec = _recovery()

    def boom(_):
        raise RuntimeError("nope")

    rec.schedule(7, boom)
    loop.handles[0].fire()  # must not raise
    assert not rec.pending(7)


def test_degenerate_config_is_sanitised():
    _, rec = _recovery(base=0, cap=-5)
    assert rec.base_delay == 1
    assert rec.max_delay == 1
    assert rec.delay_for(7) == 1
