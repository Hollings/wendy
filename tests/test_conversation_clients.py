"""Multi-turn stream transport and lifecycle, without calling a real model."""
import asyncio
import os
import sys

import pytest

from wendy import cli, prompt, state, task_auth
from wendy.conversation_clients import ClientPool
from wendy.environment import configure
from wendy.state import StateManager


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    script = tmp_path / 'fake_claude.py'
    script.write_text('''import json,sys,os,time
for line in sys.stdin:
    item=json.loads(line)
    assert item['type']=='user' and item['message']['role']=='user'
    if item['message']['content']=='crash': sys.exit(1)
    if item['message']['content']=='hang': time.sleep(120)
    print(json.dumps({'type':'system','subtype':'init','session_id':'saved-session'}),flush=True)
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':str(os.getpid())}),flush=True)
''')
    pool = ClientPool()
    saved = StateManager(tmp_path / 'state.db')
    monkeypatch.setattr(cli, 'clients', pool)
    monkeypatch.setattr(state, 'state', saved)
    monkeypatch.setattr(cli, 'CLI_SUBPROCESS_UID', None)
    monkeypatch.setattr(cli, 'WENDY_BASE', tmp_path)
    monkeypatch.setattr(cli, 'channel_dir', lambda _: tmp_path)
    monkeypatch.setattr(cli, 'session_dir', lambda _: tmp_path)
    monkeypatch.setattr(cli, 'find_cli_path', lambda: sys.executable)
    monkeypatch.setattr(cli, 'build_cli_command', lambda *args, **kwargs: [sys.executable, str(script)])
    monkeypatch.setattr(cli, '_resolve_session', lambda *args: ('saved-session', False, False))
    monkeypatch.setattr(cli, '_build_cli_env', lambda *args, **kwargs: dict(os.environ))
    monkeypatch.setattr(cli, 'setup_wendy_scripts', lambda: None)
    monkeypatch.setattr(cli, 'setup_channel_folder', lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, 'save_debug_log', lambda *args: None)
    monkeypatch.setattr(cli, 'trim_stream_log', lambda: None)
    monkeypatch.setattr(cli, 'append_to_stream_log', lambda *args: None)
    monkeypatch.setattr(prompt, 'get_journal_listing_for_nudge', lambda *args: '')
    monkeypatch.setattr(prompt, 'get_context_roster_for_nudge', lambda *args: '')
    monkeypatch.setenv('WENDY_PERSISTENT_CLIENTS', '1')
    monkeypatch.setenv('WENDY_CLIENT_IDLE_SECONDS', '600')
    async def turn(text='wake', channel=7):
        await asyncio.wait_for(cli.run_cli(channel, {'name': 'test'}, 'stable rules', nudge_override=text), 10)
    try:
        yield pool, saved, turn
    finally:
        await pool.close()


async def test_two_turns_reuse_process_and_suspend_idle_capability(runtime):
    pool, _, turn = runtime
    await turn()
    first = pool.clients[7]
    assert first.process.returncode is None
    assert task_auth.lookup(first.token) is None
    await turn('another wake')
    assert pool.clients[7] is first
    assert first.process.returncode is None
    assert not first.busy and task_auth.lookup(first.token) is None


async def test_crash_discards_client_without_resetting_session(runtime):
    pool, _, turn = runtime
    await turn()
    first = pool.clients[7]
    with pytest.raises(cli.ClaudeCliError, match='saved session retained'):
        await turn('crash')
    assert 7 not in pool.clients and task_auth.lookup(first.token) is None
    await turn()
    assert pool.clients[7].session_id == 'saved-session'
    assert pool.clients[7].process.pid != first.process.pid


async def test_cold_preference_and_idle_limit_release_only_idle_processes(runtime, monkeypatch):
    pool, saved, turn = runtime
    monkeypatch.setenv('WENDY_WARM_CLIENT_LIMIT', '1')
    await turn(channel=7)
    first = pool.clients[7]
    await turn(channel=8)
    assert 7 not in pool.clients and first.process.returncode is not None
    configure(saved, 8, 'client', 'cold')
    await turn(channel=8)
    assert not pool.clients


async def test_cancellation_releases_channel_for_next_turn(runtime):
    pool, _, turn = runtime
    task = asyncio.create_task(turn('hang'))
    for _ in range(100):
        if 7 in pool.clients:
            break
        await asyncio.sleep(0.01)
    first = pool.clients[7]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.process.returncode is not None and 7 not in pool.clients
    await turn()
    assert pool.clients[7].process.pid != first.process.pid


async def test_close_channel_prevents_stale_memory_after_external_turn(runtime):
    pool, _, turn = runtime
    await turn()
    first = pool.clients[7]
    await pool.close_channel(7)
    assert first.process.returncode is not None
    await turn()
    assert pool.clients[7].process.pid != first.process.pid
