"""Per-channel retry scheduling after transient Claude API failures.

When a turn dies on a server-side API error (529 overloaded, 5xx) and the
in-turn retry ladder in ``discord_client`` has given up, nothing used to
re-trigger the channel until a human typed again -- unread messages sat in
the queue for the rest of the outage. ``OutageRecovery`` owns a backoff
timer per channel so the bot can retry the queued messages on its own and
pick up where it left off once the API recovers.

The scheduler is deliberately dumb: it knows nothing about Discord or the
CLI. The caller supplies a callback that decides whether a retry is still
warranted when the timer fires. Attempt counters persist across retries
(so repeated failures back off further) and reset only on an explicit
``reset`` after a successful turn.

Environment:
    WENDY_RECOVERY_BASE_DELAY  seconds before the first retry   (default 120)
    WENDY_RECOVERY_MAX_DELAY   cap on the doubling backoff      (default 1800)
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

_LOG = logging.getLogger(__name__)

RECOVERY_BASE_DELAY: int = int(os.getenv("WENDY_RECOVERY_BASE_DELAY", "120"))
RECOVERY_MAX_DELAY: int = int(os.getenv("WENDY_RECOVERY_MAX_DELAY", "1800"))


class OutageRecovery:
    """Exponential-backoff retry timers keyed by channel ID."""

    def __init__(
        self,
        loop_getter: Callable[[], asyncio.AbstractEventLoop],
        *,
        base_delay: int = RECOVERY_BASE_DELAY,
        max_delay: int = RECOVERY_MAX_DELAY,
    ) -> None:
        # discord.py only attaches the real loop once the client is running,
        # so take a getter rather than a loop instance.
        self._loop_getter = loop_getter
        self.base_delay = max(1, base_delay)
        self.max_delay = max(self.base_delay, max_delay)
        self._attempts: dict[int, int] = {}
        self._handles: dict[int, asyncio.TimerHandle] = {}

    # -- inspection ---------------------------------------------------------

    def attempts(self, channel_id: int) -> int:
        """Retries scheduled for this channel since the last successful turn."""
        return self._attempts.get(channel_id, 0)

    def pending(self, channel_id: int) -> bool:
        """True if a retry timer is currently armed for the channel."""
        return channel_id in self._handles

    def delay_for(self, channel_id: int) -> int:
        """Delay the *next* ``schedule`` call would use, in seconds."""
        return min(self.base_delay * (2 ** self.attempts(channel_id)), self.max_delay)

    # -- scheduling ---------------------------------------------------------

    def schedule(self, channel_id: int, callback: Callable[[int], None]) -> int:
        """Arm a retry for ``channel_id``; returns the delay in seconds.

        Replaces any timer already armed for the channel. ``callback`` is
        invoked with the channel ID on the event loop when the timer fires.
        """
        self.cancel(channel_id)
        delay = self.delay_for(channel_id)
        self._attempts[channel_id] = self.attempts(channel_id) + 1
        self._handles[channel_id] = self._loop_getter().call_later(
            delay, self._fire, channel_id, callback,
        )
        return delay

    def _fire(self, channel_id: int, callback: Callable[[int], None]) -> None:
        self._handles.pop(channel_id, None)
        try:
            callback(channel_id)
        except Exception:
            _LOG.exception("Recovery callback failed for channel %s", channel_id)

    def cancel(self, channel_id: int) -> bool:
        """Disarm a pending retry without touching the attempt counter."""
        handle = self._handles.pop(channel_id, None)
        if handle is None:
            return False
        handle.cancel()
        return True

    def reset(self, channel_id: int) -> None:
        """Disarm and forget the channel: call after a successful turn."""
        self.cancel(channel_id)
        self._attempts.pop(channel_id, None)

    def cancel_all(self) -> None:
        """Disarm every timer (shutdown)."""
        for channel_id in list(self._handles):
            self.cancel(channel_id)
