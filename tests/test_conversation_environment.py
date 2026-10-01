"""Delivery boundaries and preferences through the real authenticated HTTP API."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wendy import api_server, task_auth
from wendy.environment import preferences
from wendy.state import StateManager


@pytest.fixture
async def conversation(tmp_path, monkeypatch):
    state = StateManager(tmp_path / 'state.db')
    state.insert_message(message_id=101, channel_id=7, guild_id=1, author_id=42,
                         author_nickname='reader', is_bot=False, content='unseen private message', timestamp=1)
    state.update_last_seen(7, 100)
    send = AsyncMock(return_value=SimpleNamespace(id=102, content='reply', attachments=[]))
    bot = SimpleNamespace(get_channel=lambda _: SimpleNamespace(send=send), _active_generations={})
    monkeypatch.setattr(api_server, 'state_manager', state)
    monkeypatch.setattr(api_server, '_discord_bot', bot)
    monkeypatch.setattr(api_server, '_save_bot_message', lambda *args: None)
    monkeypatch.setattr(api_server, 'find_attachments_for_message', lambda *args: [])
    token = task_auth.issue(role='controller', channel_id=7, active=True)
    client = TestClient(TestServer(api_server.create_app()), headers={'Authorization': f'Bearer {token}'})
    await client.start_server()
    try:
        yield state, client, token, send
    finally:
        task_auth.revoke(token)
        await client.close()


async def test_manual_send_notice_and_stop_never_read_messages(conversation):
    state, client, token, send = conversation
    response = await client.post('/api/send_message', json={'channel_id': 7, 'content': 'reply'})
    body = await response.json()
    assert body['unread_pending'] and 'unseen private message' not in json.dumps(body)
    assert state.get_last_seen(7) == 100
    send.assert_not_awaited()
    notice = await (await client.post('/api/message_delivery', json={'hook': 'PostToolUse'})).json()
    assert 'Unread messages' in notice['observation'] and 'unseen private message' not in json.dumps(notice)
    assert await (await client.post('/api/message_delivery', json={'hook': 'Stop'})).json() == {}
    await client.post('/api/send_message', json={'channel_id': 7, 'content': 'reply', 'force': True})
    assert state.get_last_seen(7) == 100
    send.assert_awaited_once()
    messages = await (await client.get('/api/check_messages/7')).json()
    assert messages['messages'][0]['content'] == 'unseen private message'
    assert state.get_last_seen(7) == 101


async def test_auto_is_opt_in_acknowledged_retriable_and_reversible(conversation):
    state, client, token, _ = conversation
    await client.post('/api/environment', json={'command': 'messages', 'value': 'auto'})
    assert preferences(state, 7)['messages'] == 'auto'
    assert preferences(state, 8)['messages'] == 'manual'
    first = await (await client.post('/api/message_delivery', json={'hook': 'PostToolUse'})).json()
    assert 'unseen private message' in first['observation']
    assert state.get_last_seen(7) == 100  # Lost HTTP response cannot silently consume it.
    again = await (await client.post('/api/message_delivery', json={'hook': 'PostToolUse'})).json()
    assert first == again
    await client.post('/api/message_delivery', json={'ack': first['delivery_id']})
    assert state.get_last_seen(7) == 101
    state.update_last_seen(7, 100)  # A failed model turn restores its checkpoint.
    await client.post('/api/environment', json={'command': 'messages', 'value': 'manual'})
    result = await (await client.post('/api/message_delivery', json={'hook': 'UserPromptSubmit'})).json()
    assert 'unseen private message' not in json.dumps(result)
    assert state.get_last_seen(7) == 100
    assert preferences(StateManager(state.db_path), 7)['messages'] == 'manual'


async def test_environment_rejects_worker_cross_channel_and_idle_capabilities(conversation):
    _, client, token, _ = conversation
    assert (await client.post('/api/environment', json={'channel_id': 8, 'command': 'status'})).status == 403
    worker = task_auth.issue(role='worker', task_key='coding:a')
    try:
        assert (await client.post('/api/environment', json={'command': 'messages', 'value': 'auto'},
                                  headers={'Authorization': f'Bearer {worker}'})).status == 403
    finally:
        task_auth.revoke(worker)
    task_auth.lookup(token)['active'] = False
    assert (await client.post('/api/environment', json={'command': 'status'})).status == 403


async def test_real_hook_and_wenv_helper_deliver_and_ack(conversation):
    state, client, token, _ = conversation
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'WENDY_API_TOKEN': token, 'WENDY_PROXY_PORT': str(client.server.port)}
    env.pop('WENDY_ENRICHMENT', None)
    proc = await asyncio.create_subprocess_exec(sys.executable, str(root / 'bin/wenv'), 'messages', 'auto',
                                                env=env, stdout=asyncio.subprocess.PIPE)
    await proc.communicate()
    assert proc.returncode == 0
    proc = await asyncio.create_subprocess_exec(sys.executable, str(root / 'config/hooks/conversation_delivery.py'),
                                                env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    output, _ = await proc.communicate(b'{"hook_event_name":"UserPromptSubmit"}')
    assert proc.returncode == 0
    assert 'unseen private message' in json.loads(output)['hookSpecificOutput']['additionalContext']
    assert state.get_last_seen(7) == 101
