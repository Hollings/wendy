"""Exercise the real CLI helper, scoped HTTP API and subprocess completion."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestServer

from wendy import api_server, task_auth, worker_runtime
from wendy.state import StateManager
from wendy.task_store import TaskStore
from wendy.tasks import TaskRunner


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != 'posix', reason='Production process-group behavior requires Linux')
async def test_stop_kills_child_even_when_parent_exits_first(tmp_path):
    child_file = tmp_path / 'child.pid'
    code = '''import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(120)
'''
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(child_file), start_new_session=True)
    try:
        for _ in range(100):
            if child_file.exists():
                break
            await asyncio.sleep(0.02)
        child_pid = int(child_file.read_text())
        await asyncio.sleep(0.1)  # Child installs its SIGTERM handler.
        await worker_runtime.stop_process(proc)
        for _ in range(100):
            stat = Path(f'/proc/{child_pid}/stat')
            if not stat.exists() or stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail('Worker child survived process-group cancellation')
    finally:
        if proc.returncode is None:
            await worker_runtime.stop_process(proc)


@pytest.mark.asyncio
async def test_worker_commands_through_http_preserve_result_and_acknowledgment(tmp_path, monkeypatch):
    store = TaskStore(StateManager(tmp_path / 'state.db'))
    queue = tmp_path / 'coding'
    queue.mkdir()
    helper = Path(__file__).resolve().parents[1] / 'bin' / 'wtask'
    script = tmp_path / 'fake_claude.py'
    script.write_text('''import json, pathlib, subprocess, sys
helper = sys.argv[1]
def call(*args):
    result = subprocess.run([sys.executable, helper, *args], capture_output=True, text=True)
    if result.returncode: raise RuntimeError(result.stderr)
    return json.loads(result.stdout)
messages = call('inbox')['messages']
for message in messages: call('ack', str(message['id']))
out = pathlib.Path.cwd() / 'progress.txt'
out.write_text('Preserved output: ' + messages[0]['text'])
call('checkpoint', 'Output written; ready to verify: ' + str(out))
assert out.read_text().startswith('Preserved output')
call('finish', 'Implemented and verified', '--artifact', str(out), '--check', 'Read back file')
print(json.dumps({'type': 'result', 'result': 'Finished'}))
''', encoding='utf-8')
    monkeypatch.setattr(worker_runtime, 'WENDY_BASE', tmp_path)
    monkeypatch.setattr(worker_runtime, 'channel_dir', lambda _: queue)
    monkeypatch.setattr(worker_runtime, 'CLI_SUBPROCESS_UID', None)
    runtime = worker_runtime.WorkerRuntime([sys.executable, str(script), str(helper)])
    runner = TaskRunner(store=store, beads=AsyncMock(), runtime=runtime)
    runner.channels = {'coding': 123}
    runner.available = True
    monkeypatch.setattr(api_server, '_discord_bot', SimpleNamespace(_task_runner=runner))
    task = store.add(bd_id='e2e', queue='coding', origin_channel=999, source_session='thread',
                     title='Fixture work', description='Write the test output', context='Reference context',
                     workspace=str(queue), model='claude-haiku-4-5-20251001')
    store.tell(task['key'], 'Latest requirement')
    server = TestServer(api_server.create_app())
    await server.start_server()
    monkeypatch.setenv('WENDY_PROXY_PORT', str(server.port))
    try:
        attempt = store.reserve(task['key'])
        store.begin_launch(attempt['id'])
        agent = await runtime.launch(task, attempt)
        runner.agents[task['key']] = agent
        store.launched(task['key'], agent.process.pid, '', str(agent.log_path))
        try:
            await asyncio.wait_for(agent.process.wait(), timeout=20)
            await runner._check_agents()
            assert store.get(task['key'])['phase'] == 'succeeded', agent.log_path.read_text()
            detail = store.detail(task['key'])
            assert detail['messages'][0]['acknowledged_at']
            assert detail['attempts'][0]['report']['verification'] == ['Read back file']
            assert (queue / 'progress.txt').read_text() == 'Preserved output: Latest requirement'
            assert task_auth.lookup(agent.token) is None
            notice = store.state.get_unseen_notifications_for_wendy()[-1]
            assert notice.channel_id == 999
            assert notice.payload['status'] == 'succeeded'
        finally:
            if agent.process.returncode is None:
                await runtime.cleanup(agent, stop=True)
    finally:
        await server.close()
