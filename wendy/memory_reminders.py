"""Optional memory reminders on natural turns, keyed by conversation, never cwd."""
import logging
import os
import sqlite3
import time
from contextlib import contextmanager

from .paths import SHARED_DIR

_LOG = logging.getLogger(__name__)


@contextmanager
def _connect():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    path = SHARED_DIR / 'memory_reminders.db'
    conn = sqlite3.connect(path, timeout=1)
    try:
        # Controller and unprivileged CLI both update these non-sensitive counters.
        # Keep them separate from the controller's capability/task databases.
        if os.name == 'posix' and path.stat().st_uid == os.geteuid():
            path.chmod(0o666)
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS reminders (channel INTEGER PRIMARY KEY, turns INTEGER DEFAULT 0, last REAL)')
            yield conn
    finally:
        conn.close()


def record_write(channel_id):
    try:
        with _connect() as conn:
            conn.execute('INSERT INTO reminders VALUES (?, 0, ?) ON CONFLICT(channel) DO UPDATE SET turns=0, last=excluded.last',
                         (channel_id, time.time()))
    except (OSError, sqlite3.Error):
        _LOG.warning('Could not record memory write for %s', channel_id)


def reminder(channel_id, journal):
    """A useful write or reminder postpones the next reminder by at least 3 hours."""
    try:
        now = time.time()
        # Also recognize journal writes made through Bash, without scanning profiles
        # belonging to other conversations. Direct profile edits use record_write.
        latest = max((p.stat().st_mtime for p in journal.iterdir() if p.is_file()), default=0) if journal.exists() else 0
        with _connect() as conn:
            conn.execute('INSERT OR IGNORE INTO reminders VALUES (?, 0, ?)', (channel_id, now))
            turns, last = conn.execute('SELECT turns, last FROM reminders WHERE channel=?', (channel_id,)).fetchone()
            if latest > last:
                turns, last = 0, latest
            turns += 1
            due = turns >= 25 and now - last >= 10800
            conn.execute('UPDATE reminders SET turns=?, last=? WHERE channel=?',
                         (0 if due else turns, now if due else last, channel_id))
        if due:
            return '[Optional memory check: save useful new information under your memory policy if needed. Skip if nothing is worth keeping. This does not require a separate turn or announcement.]'
    except (OSError, sqlite3.Error):
        _LOG.warning('Memory reminder unavailable for %s', channel_id)
    return ''
