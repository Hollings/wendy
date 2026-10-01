"""Run against the pinned BD binary in CI, in an isolated temporary database."""
import json
import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wendy import beads


@pytest.mark.asyncio
async def test_bd_timeout_stops_wrapper_and_native_process_group(tmp_path, monkeypatch):
    proc = SimpleNamespace(communicate=AsyncMock(side_effect=TimeoutError))
    launch = AsyncMock(return_value=proc)
    stop = AsyncMock()
    monkeypatch.setattr(beads.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(beads, 'stop_process', stop)
    monkeypatch.setattr(beads, 'channel_dir', lambda _: tmp_path)
    with pytest.raises(TimeoutError):
        await beads.BeadsClient().run('coding', 'ready')
    stop.assert_awaited_once_with(proc)
    assert launch.call_args.kwargs['start_new_session'] == (os.name == 'posix')


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('WENDY_TEST_BD'), reason='Set WENDY_TEST_BD to the pinned BD binary for integration testing')
async def test_pinned_bd_create_metadata_dependencies_and_status_contract(tmp_path, monkeypatch):
    binary = shutil.which(os.environ['WENDY_TEST_BD']) or os.environ['WENDY_TEST_BD']
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / '.beads').mkdir()
    monkeypatch.setattr(beads, 'channel_dir', lambda _: workspace)
    monkeypatch.setattr(beads, 'beads_dir', lambda _: workspace / '.beads')
    monkeypatch.setattr(beads, 'CLI_SUBPROCESS_UID', None)
    monkeypatch.setenv('BEADS_ACTOR', 'wendy-test')
    client = beads.BeadsClient(binary)
    assert '0.63.3' in await client.run('test', '--version')
    await client.run('test', 'init', '--skip-agents', '--skip-hooks', '--prefix', 'wt')
    origin = {'wendy': {'channel_id': 999, 'session_id': 'thread', 'request_id': 'unique'}}
    first = await client.json('test', 'create', 'schema', '-d', 'Build schema', '--assignee', 'wendy-pending',
                              '--metadata', json.dumps(origin), '-l', 'wtask-request:unique,model:claude-fable-5-1')
    second = await client.json('test', 'create', 'API', '-d', 'Build API', '--deps', first['id'])
    details = await client.show('test', first['id'])
    metadata = details['metadata']
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata == origin
    assert {t['id'] for t in await client.ready('test')} == {first['id']}
    await client.update('test', first['id'], 'blocked')
    assert await client.ready('test') == []
    await client.update('test', first['id'], 'in_progress')
    assert (await client.show('test', first['id']))['assignee'] == 'wendy-controller'
    await client.update('test', first['id'], 'closed', 'Verified')
    assert second['id'] in {t['id'] for t in await client.ready('test')}
    found = await client.json('test', 'list', '--all', '--label', 'wtask-request:unique', '--limit', '0')
    assert [item['id'] for item in found] == [first['id']]
