"""Durable agent runs, mailbox, quotas and notifications in Wendy's SQLite DB.

BD owns issue descriptions and dependency edges. These tables own execution.
No operation in this module deletes or resets workspace files.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import MODEL_MAP, resolve_model

TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    key TEXT PRIMARY KEY, bd_id TEXT NOT NULL, queue TEXT NOT NULL,
    origin_channel INTEGER NOT NULL, source_session TEXT, title TEXT NOT NULL,
    description TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', workspace TEXT NOT NULL,
    request_id TEXT,
    prerequisites TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued', attempt_id TEXT,
    note TEXT NOT NULL DEFAULT '', bd_synced TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, UNIQUE(queue, bd_id)
);
CREATE TABLE IF NOT EXISTS agent_attempts (
    id TEXT PRIMARY KEY, task_key TEXT NOT NULL, model TEXT NOT NULL,
    quota_key TEXT NOT NULL, quota_day TEXT NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'reserved',
    pid INTEGER, process_identity TEXT, log_path TEXT, checkpoint TEXT NOT NULL DEFAULT '',
    report TEXT, started_at TEXT, ended_at TEXT, last_activity TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_quota ON agent_attempts(quota_key, quota_day, status);
CREATE TABLE IF NOT EXISTS agent_mailbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL, text TEXT NOT NULL,
    created_at TEXT NOT NULL, delivered_at TEXT, acknowledged_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_runner_lease (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL, expires REAL NOT NULL
);
"""

ACTIVE_PHASES = ('starting', 'running', 'stopping', 'finishing')
RESUMABLE_PHASES = ('cancelled', 'interrupted', 'needs_input', 'timed_out')


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def quota_key(model: str) -> str:
    """Aliases, pinned versions and context suffixes share a family allowance."""
    model = resolve_model(model, allow_env_override=False).lower().split('[')[0]
    for family in MODEL_MAP:
        if model == family or model.startswith(f'claude-{family}-'):
            return family
    return model


def quota_config() -> tuple[dict[str, int], ZoneInfo]:
    raw = json.loads(os.getenv('WENDY_TASK_MODEL_LIMITS', '{"fable": 10}'))
    if not isinstance(raw, dict) or any(type(v) is not int or v < 0 for v in raw.values()):
        raise ValueError('WENDY_TASK_MODEL_LIMITS must be a JSON object of nonnegative integer limits')
    limits = {}
    for model, limit in raw.items():
        family = quota_key(model)
        limits[family] = min(limit, limits.get(family, limit))
    return limits, ZoneInfo(os.getenv('WENDY_TASK_QUOTA_TIMEZONE', 'America/Los_Angeles'))


class TaskStore:
    def __init__(self, state=None):
        if state is None:
            from .state import state
        self.state = state

    @property
    def conn(self):
        return self.state._get_conn()

    @contextmanager
    def transaction(self):
        conn = self.conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def get(self, key: str) -> dict:
        row = self.conn.execute('SELECT * FROM agent_tasks WHERE key=?', (key,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown task: {key}')
        return dict(row)

    def find(self, queue: str, task_id: str) -> dict:
        row = self.conn.execute('SELECT * FROM agent_tasks WHERE queue=? AND bd_id=?', (queue, task_id)).fetchone()
        if row is None:
            raise ValueError(f'Unknown task {task_id} in {queue}; use wtask list')
        return dict(row)

    def list(self, queue: str | None = None) -> list[dict]:
        sql = 'SELECT * FROM agent_tasks'
        args = ()
        if queue is not None:
            sql += ' WHERE queue=?'
            args = (queue,)
        return [dict(row) for row in self.conn.execute(sql + ' ORDER BY created_at', args)]

    def attempt(self, attempt_id: str | None) -> dict | None:
        row = self.conn.execute('SELECT * FROM agent_attempts WHERE id=?', (attempt_id,)).fetchone()
        return dict(row) if row else None

    def detail(self, key: str) -> dict:
        task = self.get(key)
        task['attempts'] = [dict(r) for r in self.conn.execute(
            'SELECT * FROM agent_attempts WHERE task_key=? ORDER BY rowid', (key,))]
        for attempt in task['attempts']:
            attempt['report'] = json.loads(attempt['report']) if attempt['report'] else None
        task['messages'] = [dict(r) for r in self.conn.execute(
            'SELECT * FROM agent_mailbox WHERE task_key=? ORDER BY id', (key,))]
        return task

    def add(self, *, bd_id, queue, origin_channel, source_session, title, description, context, workspace, model, request_id=None, prerequisites=None) -> dict:
        key = f'{queue}:{bd_id}'
        now = timestamp()
        with self.transaction() as conn:
            conn.execute('''INSERT OR IGNORE INTO agent_tasks
                (key, bd_id, queue, origin_channel, source_session, title, description, context,
                 workspace, model, created_at, updated_at, request_id, prerequisites) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, bd_id, queue, origin_channel, source_session, title, description, context, workspace, model, now, now,
                 request_id, json.dumps(prerequisites or [])))
        return self.get(key)

    def _event(self, conn, task: dict, status: str, summary: str = '', *, quiet: bool = False):
        # The transition and notification are committed together. No lossy outbox bridge.
        conn.execute('''INSERT INTO notifications(type, source, channel_id, title, payload)
                        VALUES (?,?,?,?,?)''',
                     ('task_started' if quiet else 'task_completion', 'task_runner', task['origin_channel'],
                      task['title'], json.dumps({'task_id': task['bd_id'], 'task_key': task['key'],
                                               'status': status, 'summary': summary})))

    def set_phase(self, key: str, phase: str, note: str = '', *, notify: bool = True):
        with self.transaction() as conn:
            task = self.get(key)
            if task['phase'] == phase and task['note'] == note:
                return
            conn.execute('UPDATE agent_tasks SET phase=?, note=?, updated_at=? WHERE key=?',
                         (phase, note, timestamp(), key))
            if notify:
                self._event(conn, task, phase, note)

    def acquire_runner(self, owner: str, now: float, ttl: float = 90) -> bool:
        with self.transaction() as conn:
            row = conn.execute('SELECT * FROM agent_runner_lease WHERE singleton=1').fetchone()
            if row and row['owner'] != owner and row['expires'] > now:
                return False
            conn.execute('INSERT OR REPLACE INTO agent_runner_lease VALUES (1,?,?)', (owner, now + ttl))
            return True

    def release_runner(self, owner: str):
        with self.transaction() as conn:
            conn.execute('DELETE FROM agent_runner_lease WHERE owner=?', (owner,))

    def models(self, now: datetime | None = None) -> dict:
        limits, zone = quota_config()
        local = (now or datetime.now(UTC)).astimezone(zone)
        reset = datetime.combine(local.date() + timedelta(days=1), time(), tzinfo=zone)
        rows = []
        for name in sorted(set(MODEL_MAP) | set(limits)):
            used = self.conn.execute('''SELECT COUNT(*) FROM agent_attempts
                WHERE quota_key=? AND quota_day=? AND (charged=1 OR status='reserved')''',
                (name, local.date().isoformat())).fetchone()[0]
            limit = limits.get(name)
            rows.append({'name': name, 'model': MODEL_MAP.get(name, name), 'limit': limit,
                         'used': used, 'remaining': max(0, limit - used) if limit is not None else None})
        return {'models': rows, 'timezone': str(zone), 'resets_at': reset.isoformat()}

    def reserve(self, key: str, *, now: datetime | None = None) -> dict | None:
        """Atomically reserve both the workspace and a global model slot."""
        limits, zone = quota_config()
        day = (now or datetime.now(UTC)).astimezone(zone).date().isoformat()
        with self.transaction() as conn:
            task = self.get(key)
            if task['phase'] not in ('queued', 'quota_wait'):
                return None
            for dependency in json.loads(task['prerequisites']):
                row = conn.execute('SELECT phase FROM agent_tasks WHERE queue=? AND bd_id=?', (task['queue'], dependency)).fetchone()
                if row and row['phase'] != 'succeeded':
                    return None
            # Conservative shared-workspace policy: one worker per channel, even
            # if descriptions name different subdirectories. Never touch git state.
            occupied = conn.execute('''SELECT 1 FROM agent_tasks WHERE queue=? AND key<>?
                AND phase IN ('starting','running','stopping','finishing')''', (task['queue'], key)).fetchone()
            if occupied:
                return None
            previous = self.attempt(task['attempt_id'])
            if previous and previous['charged']:
                # Resume is the same attempt, same model and same original quota day.
                conn.execute("UPDATE agent_attempts SET status='reserved', report=NULL, ended_at=NULL WHERE id=?", (previous['id'],))
                attempt_id = previous['id']
            else:
                family = quota_key(task['model'])
                used = conn.execute('''SELECT COUNT(*) FROM agent_attempts WHERE quota_key=? AND quota_day=?
                    AND (charged=1 OR status='reserved')''', (family, day)).fetchone()[0]
                if family in limits and used >= limits[family]:
                    if task['phase'] != 'quota_wait':
                        reset = self.models(now)['resets_at']
                        note = f'{family} daily quota exhausted; resets {reset}. Queue preserved; use wtask model to change model.'
                        conn.execute("UPDATE agent_tasks SET phase='quota_wait', note=?, updated_at=? WHERE key=?", (note, timestamp(), key))
                        self._event(conn, task, 'waiting for model quota', note)
                    return None
                attempt_id = str(uuid.uuid4())
                conn.execute('''INSERT INTO agent_attempts
                    (id, task_key, model, quota_key, quota_day, session_id) VALUES (?,?,?,?,?,?)''',
                    (attempt_id, key, task['model'], family, day, str(uuid.uuid4())))
                previous_run = conn.execute('''SELECT checkpoint, report FROM agent_attempts
                    WHERE task_key=? AND id<>? ORDER BY rowid DESC LIMIT 1''', (key, attempt_id)).fetchone()
                if previous_run:
                    checkpoint = previous_run['checkpoint']
                    if previous_run['report']:
                        checkpoint += '\nPrevious report: ' + previous_run['report']
                    conn.execute('UPDATE agent_attempts SET checkpoint=? WHERE id=?', (checkpoint, attempt_id))
            conn.execute("UPDATE agent_tasks SET phase='starting', attempt_id=?, note='', updated_at=? WHERE key=?",
                         (attempt_id, timestamp(), key))
        return self.attempt(attempt_id)

    def begin_launch(self, attempt_id: str):
        """Charge before spawning; refund only a definite failed spawn.

        A crash between OS spawn and PID persistence has an uncertain outcome.
        Conservatively retaining this charge prevents restart quota bypass.
        """
        with self.transaction() as conn:
            conn.execute("UPDATE agent_attempts SET charged=1, status='launching' WHERE id=?", (attempt_id,))

    def launched(self, key: str, pid: int, identity: str, log_path: str):
        with self.transaction() as conn:
            task = self.get(key)
            conn.execute('''UPDATE agent_attempts SET charged=1, status='running', pid=?, process_identity=?,
                log_path=?, started_at=COALESCE(started_at,?), last_activity=? WHERE id=?''',
                (pid, identity, log_path, timestamp(), timestamp(), task['attempt_id']))
            # A stop may arrive while subprocess creation yields to the API.
            if task['phase'] == 'starting':
                conn.execute("UPDATE agent_tasks SET phase='running', updated_at=? WHERE key=?", (timestamp(), key))
            self._event(conn, task, 'started', f"Model: {task['model']}. Workspace: {task['workspace']}", quiet=True)

    def launch_failed(self, key: str, reason: str, *, already_charged: bool = False):
        with self.transaction() as conn:
            task = self.get(key)
            conn.execute("UPDATE agent_attempts SET status='launch_failed', charged=?, ended_at=? WHERE id=?",
                         (int(already_charged), timestamp(), task['attempt_id']))
            conn.execute("UPDATE agent_tasks SET phase='failed', note=?, updated_at=? WHERE key=?", (reason, timestamp(), key))
            self._event(conn, task, 'failed to launch', reason + ' Files preserved. Use wtask retry.')

    def stop(self, key: str, reason: str):
        task = self.get(key)
        if task['phase'] == 'succeeded':
            raise ValueError('Task already succeeded; its result and files are preserved')
        phase = 'stopping' if task['phase'] in ACTIVE_PHASES else 'cancelled'
        self.set_phase(key, phase, reason, notify=True)

    def requeue(self, key: str, *, retry: bool = False, model: str | None = None):
        with self.transaction() as conn:
            task = self.get(key)
            if task['phase'] in ACTIVE_PHASES:
                raise ValueError('Stop the running task and wait for stopped feedback before resuming, retrying or changing models')
            if not retry and not model and task['phase'] not in RESUMABLE_PHASES:
                raise ValueError('Use resume for stopped/interrupted/needs-input tasks; use retry for a fresh attempt')
            if model:
                model = resolve_model(model, allow_env_override=False)
            fresh = retry or (model is not None and model != task['model'])
            conn.execute("UPDATE agent_tasks SET phase='queued', model=?, attempt_id=?, note='', updated_at=? WHERE key=?",
                         (model or task['model'], None if fresh else task['attempt_id'], timestamp(), key))
            self._event(conn, task, 'queued', 'Existing files and past checkpoints preserved.' +
                        (' A new attempt will consume a model slot when launched.' if fresh else ' Continuing the saved attempt.'))

    def tell(self, key: str, text: str) -> int:
        if not text.strip():
            raise ValueError('Message must not be empty')
        with self.transaction() as conn:
            task = self.get(key)
            if task['phase'] in ('finishing', 'succeeded'):
                raise ValueError('A result has already been submitted. Review it, then retry explicitly before adding new instructions.')
            cursor = conn.execute('INSERT INTO agent_mailbox(task_key,text,created_at) VALUES (?,?,?)',
                                  (key, text, timestamp()))
            return cursor.lastrowid

    def inbox(self, key: str) -> list[dict]:
        with self.transaction() as conn:
            conn.execute('UPDATE agent_mailbox SET delivered_at=COALESCE(delivered_at,?) WHERE task_key=? AND acknowledged_at IS NULL',
                         (timestamp(), key))
            task = self.get(key)
            conn.execute('UPDATE agent_attempts SET last_activity=? WHERE id=?', (timestamp(), task['attempt_id']))
            return [dict(r) for r in conn.execute('SELECT * FROM agent_mailbox WHERE task_key=? AND acknowledged_at IS NULL ORDER BY id', (key,))]

    def ack(self, key: str, message_id: int):
        with self.transaction() as conn:
            cursor = conn.execute('''UPDATE agent_mailbox SET acknowledged_at=COALESCE(acknowledged_at,?)
                WHERE id=? AND task_key=? AND delivered_at IS NOT NULL''', (timestamp(), message_id, key))
            if not cursor.rowcount:
                raise ValueError('Read this task message with inbox before acknowledging it')

    def checkpoint(self, key: str, text: str):
        with self.transaction() as conn:
            task = self.get(key)
            conn.execute('UPDATE agent_attempts SET checkpoint=?, last_activity=? WHERE id=?',
                         (text, timestamp(), task['attempt_id']))

    def report(self, key: str, report: dict):
        if report.get('outcome') not in ('succeeded', 'failed', 'needs_input'):
            raise ValueError('Outcome must be succeeded, failed, or needs_input')
        if not isinstance(report.get('summary'), str) or not report['summary'].strip():
            raise ValueError('A nonempty summary is required')
        for field in ('artifacts', 'verification', 'remaining'):
            if not isinstance(report.get(field, []), list) or any(not isinstance(x, str) for x in report.get(field, [])):
                raise ValueError(f'{field} must be an array of strings')
        with self.transaction() as conn:
            task = self.get(key)
            if task['phase'] not in ('running', 'finishing'):
                raise ValueError('Task is no longer accepting a result; preserve files and exit')
            pending = conn.execute('SELECT 1 FROM agent_mailbox WHERE task_key=? AND acknowledged_at IS NULL', (key,)).fetchone()
            if pending:
                raise ValueError('Read and acknowledge outstanding corrections before submitting a result')
            conn.execute('UPDATE agent_attempts SET report=?, last_activity=? WHERE id=?',
                         (json.dumps(report), timestamp(), task['attempt_id']))
            conn.execute("UPDATE agent_tasks SET phase='finishing', updated_at=? WHERE key=?", (timestamp(), key))

    def finish(self, key: str, outcome: str, summary: str):
        with self.transaction() as conn:
            task = self.get(key)
            if task['phase'] not in ACTIVE_PHASES:
                return  # Idempotent completion and notification.
            conn.execute('UPDATE agent_attempts SET status=?, ended_at=? WHERE id=?',
                         (outcome, timestamp(), task['attempt_id']))
            conn.execute('UPDATE agent_tasks SET phase=?, note=?, updated_at=? WHERE key=?',
                         (outcome, summary, timestamp(), key))
            self._event(conn, task, outcome, summary)

    def pending_bd_updates(self) -> list[tuple[dict, str]]:
        updates = []
        for task in self.list():
            desired = ('closed' if task['phase'] == 'succeeded' else
                       'in_progress' if task['phase'] in ACTIVE_PHASES else
                       'open' if task['phase'] in ('queued', 'quota_wait') else 'blocked')
            if task['bd_synced'] != desired:
                updates.append((task, desired))
        return updates

    def mark_bd_synced(self, key: str, status: str):
        with self.transaction() as conn:
            conn.execute('UPDATE agent_tasks SET bd_synced=? WHERE key=?', (status, key))
