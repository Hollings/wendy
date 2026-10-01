"""Behavioral tests for durable task execution, quotas, delivery and recovery."""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wendy import api_server, task_auth
from wendy.beads import BeadsError
from wendy.config import MODEL_MAP
from wendy.state import StateManager
from wendy.task_store import TaskStore, quota_key
from wendy.tasks import TaskRunner, select_task_model


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('WENDY_TASK_MODEL_LIMITS', '{"fable":3}')
    monkeypatch.setenv('WENDY_TASK_QUOTA_TIMEZONE', 'America/Los_Angeles')
    return TaskStore(StateManager(tmp_path / 'state.db'))


def add(store, ident='one', queue='coding', model='fable', origin=123):
    return store.add(bd_id=ident, queue=queue, origin_channel=origin, source_session='origin-session',
                     title='Implement change', description='Change the requested file and verify it.',
                     context='Original immutable brief', workspace=str(store.state.db_path.parent / queue),
                     model=select_task_model(model))


def start(store, task, now=None):
    attempt = store.reserve(task['key'], now=now)
    assert attempt is not None
    store.begin_launch(attempt['id'])
    store.launched(task['key'], 12345, 'fake-identity', '/logs/run.log')
    return store.attempt(attempt['id'])


def fable(store, now=None):
    return next(m for m in store.models(now)['models'] if m['name'] == 'fable')


def test_fable_51_and_explicit_worker_model_ignore_conversation_override(monkeypatch):
    monkeypatch.setenv('WENDY_MODEL_OVERRIDE', 'haiku')
    assert MODEL_MAP['fable'] == 'claude-fable-5-1'
    assert select_task_model('fable') == 'claude-fable-5-1'
    assert select_task_model('opus') == MODEL_MAP['opus']
    assert quota_key('claude-fable-5-1[1m]') == quota_key('fable')
    with pytest.raises(ValueError):
        select_task_model('unknown-alias')


def test_quota_global_across_channels_aliases_and_restarts(store):
    for i, model in enumerate(('fable', 'claude-fable-5-1', 'claude-fable-5')):
        task = add(store, str(i), queue=f'queue{i}', model=model)
        start(store, task)
        store.finish(task['key'], 'failed', 'Work was attempted')
    restarted = TaskStore(StateManager(store.state.db_path))
    last = add(restarted, 'fourth', queue='another')
    assert restarted.reserve(last['key']) is None
    assert restarted.get(last['key'])['phase'] == 'quota_wait'
    assert fable(restarted)['remaining'] == 0
    notices = restarted.state.get_unseen_notifications_for_wendy()
    n = len(notices)
    assert restarted.reserve(last['key']) is None
    assert len(restarted.state.get_unseen_notifications_for_wendy()) == n


def test_parallel_reservations_cannot_exceed_three(store):
    keys = [add(store, str(i), queue=f'q{i}')['key'] for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(store.reserve, keys))
    assert sum(result is not None for result in results) == 3
    assert fable(store)['used'] == 3


def test_shared_workspace_serializes_workers_even_on_different_models(store):
    first = add(store)
    second = add(store, 'two', model='haiku')
    start(store, first)
    assert store.reserve(second['key']) is None
    store.finish(first['key'], 'cancelled', 'Stopped')
    assert store.reserve(second['key']) is not None


def test_spawn_failure_refunds_slot_and_retry_keeps_previous_checkpoint(store):
    task = add(store)
    attempt = store.reserve(task['key'])
    store.checkpoint(task['key'], 'Useful progress from previous work')
    store.begin_launch(attempt['id'])
    store.launch_failed(task['key'], 'executable missing')
    assert fable(store)['used'] == 0
    store.requeue(task['key'], retry=True)
    retry = store.reserve(task['key'])
    assert retry['id'] != attempt['id']
    assert 'Useful progress' in retry['checkpoint']


def test_resume_same_attempt_and_model_switch_new_attempt(store):
    task = add(store)
    original = start(store, task)
    store.checkpoint(task['key'], 'File saved, tests pending')
    store.finish(task['key'], 'interrupted', 'Restart')
    store.requeue(task['key'])
    resumed = store.reserve(task['key'])
    assert resumed['id'] == original['id']
    assert resumed['session_id'] == original['session_id']
    assert resumed['checkpoint'] == 'File saved, tests pending'
    assert fable(store)['used'] == 1
    store.finish(task['key'], 'cancelled', 'Stop before changing model')
    store.requeue(task['key'], model='opus')
    switched = store.reserve(task['key'])
    assert switched['id'] != original['id']
    assert switched['model'] == MODEL_MAP['opus']
    assert fable(store)['used'] == 1


def test_daily_reset_uses_pacific_midnight_and_handles_dst(store):
    before = datetime(2026, 9, 6, 6, 59, tzinfo=UTC)
    after = datetime(2026, 9, 6, 7, 0, tzinfo=UTC)
    task = add(store)
    start(store, task, before)
    assert fable(store, before)['used'] == 1
    assert fable(store, after)['used'] == 0
    assert store.models(before)['resets_at'] == '2026-09-06T00:00:00-07:00'
    fall = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)
    assert store.models(fall)['resets_at'] == '2026-11-02T00:00:00-08:00'


def test_cancellation_preserves_files_and_consumed_quota(store, tmp_path):
    output = tmp_path / 'untracked-progress.txt'
    output.write_text('irreplaceable work')
    task = add(store)
    start(store, task)
    store.stop(task['key'], 'User stopped task')
    store.finish(task['key'], 'cancelled', 'Stopped')
    assert output.read_text() == 'irreplaceable work'
    assert fable(store)['used'] == 1
    assert store.pending_bd_updates()[0][1] == 'blocked'


def test_mailbox_delivery_is_distinct_from_ack_and_gates_report(store):
    task = add(store)
    start(store, task)
    mid = store.tell(task['key'], 'Use the new requirement')
    assert store.detail(task['key'])['messages'][0]['delivered_at'] is None
    with pytest.raises(ValueError, match='Read this task message'):
        store.ack(task['key'], mid)
    inbox = store.inbox(task['key'])
    assert inbox[0]['delivered_at']
    assert inbox[0]['acknowledged_at'] is None
    report = {'outcome': 'succeeded', 'summary': 'Done', 'verification': ['tests passed']}
    with pytest.raises(ValueError, match='corrections'):
        store.report(task['key'], report)
    store.ack(task['key'], mid)
    store.report(task['key'], report)
    with pytest.raises(ValueError, match='already been submitted'):
        store.tell(task['key'], 'Too late for this result')


def test_result_and_notification_atomic_and_finish_idempotent(store):
    task = add(store, origin=999)
    attempt = start(store, task)
    store.report(task['key'], {'outcome': 'needs_input', 'summary': 'Which file?'})
    store.conn.execute("CREATE TRIGGER fail_notify BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
    store.conn.commit()
    with pytest.raises(Exception, match='disk fault'):
        store.finish(task['key'], 'needs_input', 'Which file?')
    assert store.get(task['key'])['phase'] == 'finishing'
    assert store.attempt(attempt['id'])['ended_at'] is None
    store.conn.execute('DROP TRIGGER fail_notify')
    store.conn.commit()
    store.finish(task['key'], 'needs_input', 'Which file?')
    store.finish(task['key'], 'needs_input', 'Which file?')
    notices = [n for n in store.state.get_unseen_notifications_for_wendy() if n.type == 'task_completion']
    assert len(notices) == 1
    assert notices[0].channel_id == 999
    assert notices[0].payload['status'] == 'needs_input'


def test_notifications_retry_without_duplicates_until_successful_turn(store):
    task = add(store, origin=999)
    start(store, task)
    store.finish(task['key'], 'failed', 'Preserved report')
    notice = store.state.get_unseen_notifications_for_wendy()[-1]
    sm = store.state
    sm.materialize_task_notification(notice.id, 999, 'result')
    sm.materialize_task_notification(notice.id, 999, 'result')
    assert sm._get_conn().execute('SELECT COUNT(*) FROM message_history').fetchone()[0] == 1
    message_id = 9_000_000_000_000_000_000 + notice.id
    sm.mark_synthetics_delivered([message_id])
    assert not sm.materialize_task_notification(notice.id, 999, 'result')
    sm.rollback_delivered_synthetics(999)
    assert sm.materialize_task_notification(notice.id, 999, 'result')
    sm.mark_synthetics_delivered([message_id])
    sm.commit_delivered_synthetics(999)
    assert all(n.id != notice.id for n in sm.get_unseen_notifications_for_wendy())


def test_unseen_task_notifications_survive_cleanup(store):
    task = add(store)
    start(store, task)
    store.finish(task['key'], 'failed', 'Important')
    store.state.cleanup_old_notifications(keep_count=0)
    assert len(store.state.get_unseen_notifications_for_wendy()) == 2


def test_runner_lease_excludes_second_owner_until_expired(store):
    assert store.acquire_runner('a', 100, 90)
    assert not store.acquire_runner('b', 150, 90)
    assert store.acquire_runner('b', 191, 90)
    store.release_runner('a')
    assert not store.acquire_runner('c', 192, 90)


@pytest.fixture
def runner(store):
    instance = TaskRunner(store=store, beads=AsyncMock(), runtime=AsyncMock())
    instance.channels = {'coding': 123}
    instance.beads.json.return_value = []
    instance.available = True
    return instance


def worker(runner, task, *, exit_code=None):
    attempt = start(runner.store, task)
    agent = SimpleNamespace(process=SimpleNamespace(returncode=exit_code, pid=12345),
                            log_path=Path('missing.log'), started=time.monotonic(), report_seen=None)
    runner.agents[task['key']] = agent
    return attempt, agent


@pytest.mark.asyncio
async def test_not_done_is_needs_input_not_success(runner):
    task = add(runner.store)
    worker(runner, task, exit_code=0)
    runner.store.report(task['key'], {'outcome': 'needs_input', 'summary': 'not done: missing spec'})
    await runner._check_agents()
    assert runner.store.get(task['key'])['phase'] == 'needs_input'
    assert not runner.agents


@pytest.mark.asyncio
async def test_clean_exit_without_report_fails_honestly(runner):
    task = add(runner.store)
    worker(runner, task, exit_code=0)
    await runner._check_agents()
    assert runner.store.get(task['key'])['phase'] == 'failed'


@pytest.mark.asyncio
async def test_stop_wins_over_previously_submitted_success(runner):
    task = add(runner.store)
    worker(runner, task)
    runner.store.report(task['key'], {'outcome': 'succeeded', 'summary': 'Done'})
    runner.store.stop(task['key'], 'User cancelled')
    await runner._check_agents()
    assert runner.store.get(task['key'])['phase'] == 'cancelled'
    runner.runtime.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_bd_outages_never_kill_worker(runner):
    task = add(runner.store)
    worker(runner, task)
    runner.beads.show.side_effect = BeadsError('database unavailable')
    for _ in range(4):
        await runner._check_agents()
    assert task['key'] in runner.agents
    runner.runtime.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_retains_closed_results_and_holds_unfinished_work(runner):
    done = add(runner.store, 'done')
    start(runner.store, done)
    runner.store.report(done['key'], {'outcome': 'succeeded', 'summary': 'Verified'})
    runner.store.finish(done['key'], 'succeeded', 'Verified')
    pending = add(runner.store, 'unfinished')
    start(runner.store, pending)
    runner.store.checkpoint(pending['key'], 'Half implemented')
    await runner._recover()
    assert runner.store.get(done['key'])['phase'] == 'succeeded'
    assert runner.store.get(pending['key'])['phase'] == 'interrupted'
    assert fable(runner.store)['used'] == 2
    assert 'Half implemented' in runner.store.detail(pending['key'])['attempts'][0]['checkpoint']


@pytest.mark.asyncio
async def test_start_records_thread_and_context_at_submission(runner):
    runner.beads.json.side_effect = [[], {'id': 'bd-thread'}]
    runner.store.state.insert_message(100, 999, None, 7, 'Alice', False, 'Original request', 100)
    scope = {'role': 'controller', 'queue': 'coding', 'channel_id': 999, 'session_id': 'thread-session'}
    response = await runner.command(scope, {'command': 'start', 'title': 'Task', 'description': 'Do it', 'model': 'fable'})
    task = response['task']
    runner.store.state.insert_message(101, 999, None, 7, 'Alice', False, 'Later request', 101)
    assert task['origin_channel'] == 999
    assert task['source_session'] == 'thread-session'
    assert 'Original request' in task['context']
    assert 'Later request' not in task['context']
    assert 'wendy-pending' in runner.beads.json.call_args.args


@pytest.mark.asyncio
async def test_worker_cannot_manage_other_tasks_or_use_old_attempt(runner):
    task = add(runner.store)
    attempt, _ = worker(runner, task)
    scope = {'role': 'worker', 'task_key': task['key'], 'attempt_id': attempt['id']}
    with pytest.raises(ValueError, match='Workers may only'):
        await runner.command(scope, {'command': 'start', 'title': 'Forbidden'})
    scope['attempt_id'] = 'stale'
    with pytest.raises(ValueError, match='no longer active'):
        await runner.command(scope, {'command': 'checkpoint', 'text': 'overwrite'})


@pytest.mark.asyncio
async def test_api_blocks_anonymous_worker_and_cross_channel_messaging():
    handler = AsyncMock(return_value='ok')
    request = SimpleNamespace(path='/api/send_message', headers={}, match_info={}, content_type='application/json',
                              json=AsyncMock(return_value={'channel_id': 123}))
    assert (await api_server.controller_capability(request, handler)).status == 403
    token = task_auth.issue(role='worker', task_key='coding:one', attempt_id='1')
    request.headers['Authorization'] = f'Bearer {token}'
    assert (await api_server.controller_capability(request, handler)).status == 403
    task_auth.revoke(token)
    token = task_auth.issue(role='controller', channel_id=123, queue='coding')
    request.headers['Authorization'] = f'Bearer {token}'
    assert await api_server.controller_capability(request, handler) == 'ok'
    request.json.return_value = {'channel_id': 999}
    assert (await api_server.controller_capability(request, handler)).status == 403
    task_auth.revoke(token)


@pytest.mark.asyncio
async def test_submission_retry_is_idempotent_and_dependencies_are_atomic(runner):
    runner.beads.json.side_effect = [[], {'id': 'deduplicated'}]
    runner.beads.show.return_value = {'id': 'parent'}
    scope = {'role': 'controller', 'queue': 'coding', 'channel_id': 999, 'session_id': 'thread'}
    body = {'command': 'start', 'title': 'Task', 'description': 'Specification', 'model': 'fable',
            'request_id': str(uuid.uuid4()), 'after': ['parent']}
    first = await runner.command(scope, body)
    again = await runner.command(scope, body)
    assert first['task']['key'] == again['task']['key']
    assert again['reused']
    assert runner.beads.json.await_count == 2  # One lookup and one create, no second issue.
    create_args = runner.beads.json.call_args.args
    assert create_args[create_args.index('--deps') + 1] == 'parent'
    with pytest.raises(ValueError, match='different request'):
        await runner.command(scope, {**body, 'description': 'Changed request'})


@pytest.mark.asyncio
async def test_scheduler_skips_exhausted_model_but_runs_available_model(runner):
    for i in range(3):
        used = add(runner.store, str(i), queue=f'other{i}')
        start(runner.store, used)
        runner.store.finish(used['key'], 'failed', 'Attempted')
    expensive = add(runner.store, 'expensive')
    cheap = add(runner.store, 'cheap', model='haiku')
    runner.beads.ready.return_value = [{'id': expensive['bd_id']}, {'id': cheap['bd_id']}]
    runner.runtime.launch.return_value = SimpleNamespace(process=SimpleNamespace(pid=12345), log_path=Path('/logs/test'))
    await runner._schedule()
    assert runner.store.get(expensive['key'])['phase'] == 'quota_wait'
    assert runner.store.get(cheap['key'])['phase'] == 'running'
    assert runner.runtime.launch.await_count == 1
    assert runner.runtime.launch.call_args.args[0]['key'] == cheap['key']


@pytest.mark.asyncio
async def test_worker_inbox_does_not_trigger_bd_rescan(runner):
    task = add(runner.store)
    attempt, _ = worker(runner, task)
    scope = {'role': 'worker', 'task_key': task['key'], 'attempt_id': attempt['id']}
    await runner.command(scope, {'command': 'inbox'})
    assert not runner.wake.is_set()


def test_prerequisite_retry_blocks_child_even_if_bd_ready_is_stale(store):
    parent = add(store, 'parent')
    child = store.add(bd_id='child', queue='coding', origin_channel=123, source_session=None,
                      title='Child', description='Use parent output', context='', workspace='/shared',
                      model=MODEL_MAP['haiku'], prerequisites=['parent'])
    start(store, parent)
    store.finish(parent['key'], 'succeeded', 'First attempt done')
    store.requeue(parent['key'], retry=True)
    assert store.reserve(child['key']) is None


def test_uncertain_launch_crash_preserves_quota_charge(store):
    task = add(store)
    attempt = store.reserve(task['key'])
    store.begin_launch(attempt['id'])
    store.finish(task['key'], 'interrupted', 'Crashed before PID persistence')
    assert fable(store)['used'] == 1
