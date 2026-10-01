"""Memory boundaries, replay, citations, and the complete MCP-to-source path."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memory_protocol import ResearchRequest, Scope, Source, read_limited
from services.memory.gateway import Gateway
from services.memory.hindsight import Hindsight
from services.memory.index import Index
from services.memory.researcher import Answer, Researcher, command, validate
from services.memory.server import RESEARCHER, create_app
from wendy import api_server, memory_export, task_auth
from wendy.cli import build_cli_command
from wendy.memory_api import EXPORTER
from wendy.memory_backfill import import_channel
from wendy.memory_export import Exporter, resolve_scope
from wendy.state import StateManager


def source(id='101', text='Ada chose Postgres for the journal project.', channel='7', **extra):
    return Source(id='discord:' + id, domain='channel:' + channel, kind='chat', text=text,
                  speaker='Ada', timestamp=1700000000, channel_id=channel, message_id=id,
                  location='test', locator=f'https://discord.com/channels/1/{channel}/{id}', **extra)


def scope(cutoff='101', domains=None):
    return Scope(origin='7', domains=domains or ['channel:7'], cutoffs={'7': cutoff}, policy='test')


@pytest.fixture
def index(tmp_path):
    result = Index(tmp_path / 'memory.db')
    yield result
    result.db.close()


class Backend:
    async def run(self, index):
        await asyncio.Event().wait()

    async def recall(self, index, scope, query):
        return {'leads': [], 'unavailable': True}


async def runner(request, gateway, token, url, previous=None):
    # Uses the actual narrow HTTP capability, not direct access to the database.
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers={'Authorization': 'Bearer ' + token},
                                json={'tool': 'search_sources', 'arguments': {'query': 'Postgres'}}) as response:
            record = (await response.json())['sources'][0]
    return Answer(status='answered', answer='Ada chose Postgres. [S1]', citations=[{
        'ref': 'S1', 'source_id': record['id'], 'revision': record['revision'],
        'excerpt': 'Ada chose Postgres',
    }], limitations=[])


def test_outbox_is_transactional_and_catches_old_ids_edits_deletes(tmp_path):
    state = StateManager(tmp_path / 'bot.db')
    for id in (200, 100, 9_000_000_000_000_000_001):
        state.insert_message(id, 7, 1, 42, 'Ada', False, 'old', 1)
    assert state.memory_version() == 2
    state.update_message_content(100, 'corrected')
    state.delete_messages([200])
    changes = state.memory_changes(2)
    assert [(r['seq'], r['changed_id'], r['content']) for r in changes] == [(3, 100, 'corrected'), (4, 200, None)]
    conn = state._get_conn()
    conn.execute('DELETE FROM message_history WHERE message_id=100')
    conn.rollback()
    assert state.memory_version() == 4  # Mutation and outbox rollback together.


@pytest.mark.parametrize('timestamp,expected', [
    (None, 0), (1700000000, 1700000000), ('1700000000', 1700000000),
    ('2023-11-14T22:13:20Z', 1700000000),
    ('2023-11-14T22:13:20', 1700000000),
    ('2023-11-14T15:13:20.221000-07:00', 1700000000),
])
def test_message_source_accepts_legacy_cache_timestamps(tmp_path, timestamp, expected):
    state = StateManager(tmp_path / 'bot.db')
    state.insert_message(101, 7, 1, 42, 'Ada', False, 'Historical message', timestamp)
    record = memory_export.message_source(state.memory_page()[0], {'7': 'test'})
    assert record.timestamp == expected


def test_scope_search_cutoffs_neighbors_and_whole_episode_provenance(index):
    index.ingest([source(), source('102', 'unread violet elephant'), source('103', 'private Postgres', '8')], [])
    assert [s.id for s in index.search('Postgres', scope())] == ['discord:101']
    assert index.search('violet elephant', scope()) == []
    assert index.neighbors(source(), scope()) == []
    with index.db:
        index.db.execute('UPDATE documents SET synced=revision')
    doc = index.db.execute("SELECT id FROM documents WHERE domain='channel:7'").fetchone()[0]
    assert index.document_sources(doc, scope()) == []
    assert len(index.document_sources(doc, scope('102'))) == 2
    index.ingest([source('101', 'Correction: SQLite')], [])
    assert index.document_sources(doc, scope('102')) == []
    assert index.search('Postgres', scope()) == []


async def test_citations_require_read_current_original_and_exact_excerpt(index):
    index.ingest([source()], [])
    gateway = Gateway(index, Backend(), scope(), 'standard')
    result = await gateway.call('read_sources', {'source_ids': ['discord:101']})
    record = result['sources'][0]
    answer = Answer(status='answered', answer='Ada chose Postgres [S1]', citations=[{
        'ref': 'S1', 'source_id': record['id'], 'revision': record['revision'], 'excerpt': 'Ada chose Postgres'}], limitations=[])
    assert validate(answer, gateway, ResearchRequest(question='Database?'))[0][0]['speaker'] == 'Ada'
    answer.citations[0].excerpt = 'Ada chose MySQL'
    with pytest.raises(ValueError, match='Excerpt'):
        validate(answer, gateway, ResearchRequest(question='Database?'))
    answer.citations[0].excerpt = 'Ada chose Postgres'
    index.ingest([], ['discord:101'])
    with pytest.raises(ValueError, match='Source'):
        validate(answer, gateway, ResearchRequest(question='Database?'))


async def test_budget_and_capability_revocation_on_cancellation(index):
    ready = asyncio.Event()

    async def blocked(*args):
        ready.set()
        await asyncio.Event().wait()

    researcher = Researcher(index, Backend(), 'unused', blocked)
    task = asyncio.create_task(researcher.research(ResearchRequest(question='Why?'), scope()))
    await ready.wait()
    assert len(researcher.capabilities) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not researcher.capabilities and researcher.active == 0
    gateway = Gateway(index, Backend(), scope(), 'standard')
    for _ in range(8):
        await gateway.call('inspect_coverage', {})
    assert 'error' in await gateway.call('search_sources', {'query': 'anything'})


async def test_chunked_http_responses_and_hard_limit():
    class Stream:
        def __init__(self):
            self.parts = iter([b'{"', b'ok":', b'true}', b''])

        async def read(self, n):
            return next(self.parts)

    assert json.loads(await read_limited(Stream(), 20)) == {'ok': True}
    with pytest.raises(ValueError, match='byte limit'):
        await read_limited(Stream(), 5)


async def test_receipts_exclude_sources_removed_by_output_budget(index):
    index.ingest([source(str(i), text='Postgres ' + 'x' * 4000) for i in range(90, 102)], [])
    gateway = Gateway(index, Backend(), scope(), 'standard')
    gateway.remaining = 6000
    result = await gateway.call('search_sources', {'query': 'Postgres'})
    assert result['truncated']
    assert set(gateway.seen) == {s['id'] for s in result['sources']}


def test_domain_change_and_origin_cutoff_keep_cross_channel_history(tmp_path, monkeypatch):
    state = StateManager(tmp_path / 'bot.db')
    for channel in (7, 8):
        state.insert_message(channel * 100, channel, 1, 42, 'Ada', False, 'history', 1)
    state.update_last_seen(7, 600)
    config = {'cross_channel': True, 'include_profiles': False, 'channels': []}
    monkeypatch.setattr(memory_export, 'settings', lambda: config)
    configs = {7: {'name': 'a'}, 8: {'name': 'b'}}
    granted = resolve_scope(state, configs, 7)
    assert granted.cutoffs == {'7': '600', '8': '800'}
    config['cross_channel'] = False
    narrowed = resolve_scope(state, configs, 7)
    assert narrowed.domains == ['channel:7'] and narrowed.policy != granted.policy


async def test_backend_modes_keep_evidence_reads_available(index):
    index.ingest([source()], [])
    gateway = Gateway(index, Backend(), scope(), 'standard', mode='hindsight')
    assert 'disabled' in await gateway.call('search_sources', {'query': 'Postgres'})
    assert (await gateway.call('read_sources', {'source_ids': ['discord:101']}))['sources']
    gateway = Gateway(index, Backend(), scope(), 'standard', mode='sources')
    assert 'disabled' in await gateway.call('recall_memory', {'query': 'Postgres'})


async def test_coverage_counts_are_not_source_receipts(index):
    index.ingest([source()], [])
    gateway = Gateway(index, Backend(), scope(), 'standard')
    result = await gateway.call('inspect_coverage', {})
    assert result['sources'] == {'chat': 1}
    assert gateway.seen == {} and gateway.events[-1]['source_ids'] == []


def test_researcher_and_controller_have_explicit_disjoint_tools(monkeypatch):
    argv = command('claude', Path('mcp.json'), 'research', 'sonnet', 10)
    assert argv[argv.index('--tools') + 1] == ''
    assert '--restricted' in argv and '--no-session-persistence' in argv
    assert argv[argv.index('--setting-sources') + 1] == ''
    assert argv[argv.index('--permission-mode') + 1] == 'dontAsk'
    monkeypatch.setenv('WENDY_MEMORY_ENABLED', 'true')
    main = build_cli_command('claude', 'session', True, '', {'name': 'test'}, 'sonnet')
    assert 'mcp__memory__research_memory' in main[main.index('--allowedTools') + 1]
    config = json.loads(main[main.index('--mcp-config') + 1])
    assert set(config['mcpServers']) == {'memory'}
    assert 'WENDY_MEMORY_SERVICE_TOKEN' not in json.dumps(config)


async def test_hindsight_pending_revision_survives_edit_and_lost_ack(index):
    index.ingest([source()], [])
    backend = Hindsight(None, 'http://unused')
    calls, fail = [], True

    async def remote(method, domain, suffix='', payload=None, missing_ok=False):
        nonlocal fail
        calls.append((method, suffix, payload))
        if suffix == '/memories' and fail:
            fail = False
            raise TimeoutError()
        return {'status': 'completed'}

    backend.request = remote
    await backend.sync_one(index)  # Persist pending payload.
    original = dict(index.db.execute('SELECT * FROM documents').fetchone())
    await backend.sync_one(index)  # Unknown acknowledgment.
    index.ingest([source(text='Ada corrected the decision to SQLite.')], [])
    with index.db:
        index.db.execute('UPDATE documents SET retry_at=0')
    await backend.sync_one(index)  # Same operation/payload; marks only old revision complete.
    row = dict(index.db.execute('SELECT * FROM documents').fetchone())
    assert row['synced'] != row['revision']
    submitted = [c[2] for c in calls if c[1] == '/memories']
    assert submitted[0] == submitted[1]
    assert submitted[0]['operation_id'] == original['operation']
    assert not index.document_sources(row['id'], scope())
    await backend.sync_one(index)
    assert index.db.execute('SELECT operation FROM documents').fetchone()[0] != original['operation']


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv('WENDY_MEMORY_SERVICE_TOKEN', 's' * 40)
    monkeypatch.setenv('WENDY_MEMORY_ENABLED', 'true')
    config = {'cross_channel': False, 'include_profiles': True, 'channels': [], 'sync_seconds': 3600,
              'file_scan_seconds': 60, 'backfill': False}
    monkeypatch.setattr(memory_export, 'settings', lambda: config)
    monkeypatch.setattr(memory_export, 'WENDY_BASE', tmp_path)
    monkeypatch.setattr(memory_export, 'journal_dir', lambda folder: tmp_path / 'channels' / folder / 'journal')
    state = StateManager(tmp_path / 'bot.db')
    state.insert_message(101, 7, 1, 42, 'Ada', False, 'Ada chose Postgres for the journal project.', 1700000000)
    state.insert_message(102, 7, 1, 42, 'Ada', False, 'unread secret', 1700000001)
    state.update_last_seen(7, 101)
    index = Index(tmp_path / 'index.db')
    service = TestServer(create_app(index, Backend(), runner))
    await service.start_server()
    service.app[RESEARCHER].gateway_url = str(service.make_url('/v1/retrieve'))
    monkeypatch.setenv('WENDY_MEMORY_URL', str(service.make_url('')).rstrip('/'))
    monkeypatch.setattr(api_server, 'state_manager', state)
    monkeypatch.setattr(api_server, '_channel_configs', {7: {'name': 'test'}})
    token = task_auth.issue(role='controller', channel_id=7, active=True)
    bot = TestClient(TestServer(api_server.create_app()), headers={'Authorization': 'Bearer ' + token})
    await bot.start_server()
    try:
        yield state, index, service, bot, token, config
    finally:
        task_auth.revoke(token)
        await bot.close()
        await service.close()
        index.db.close()


async def test_real_stdio_mcp_through_bot_researcher_gateway_index(stack, monkeypatch, tmp_path):
    state, index, service, bot, token, config = stack
    # Production's workspace has its own secrets.py helper. Launch with the
    # actual controller configuration and ensure it cannot shadow stdlib imports.
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'secrets.py').write_text('raise RuntimeError("Workspace secrets.py was imported")\n')
    argv = build_cli_command('claude', 'session', True, '', {'name': 'test'}, 'sonnet')
    server = json.loads(argv[argv.index('--mcp-config') + 1])['mcpServers']['memory']
    params = StdioServerParameters(command=server['command'], args=server['args'], cwd=str(workspace), env={
        **server['env'], 'WENDY_API_TOKEN': token, 'WENDY_PROXY_PORT': str(bot.server.port),
    })
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == {'research_memory', 'open_memory_evidence'}
        result = await session.call_tool('research_memory', {'question': 'What database did Ada choose?'})
        assert not result.isError
        assert len(result.content) == 1 and result.structuredContent is None
        text = result.content[0].text
        assert 'Ada chose Postgres' in text and '[S1]' in text and 'unread secret' not in text
    assert state.get_last_seen(7) == 101
    row = index.db.execute('SELECT id FROM research').fetchone()
    response = await bot.post('/api/memory/evidence', json={'research_id': row[0], 'refs': ['S1']})
    assert (await response.json())['sources'][0]['text'].startswith('Ada chose')
    state.delete_messages([101])
    response = await bot.post('/api/memory/evidence', json={'research_id': row[0], 'refs': ['S1']})
    assert response.status == 503


async def test_worker_forged_scope_and_expired_retrieval_rejected(stack):
    _, _, service, bot, _, _ = stack
    worker = task_auth.issue(role='worker', channel_id=7)
    try:
        response = await bot.post('/api/memory/research', json={'question': 'anything'},
                                  headers={'Authorization': 'Bearer ' + worker})
        assert response.status == 403
    finally:
        task_auth.revoke(worker)
    response = await bot.post('/api/memory/research', json={'question': 'anything', 'scope': {'domains': ['secret']}})
    assert response.status == 400
    async with aiohttp.ClientSession() as session:
        response = await session.post(service.make_url('/v1/retrieve'), json={'tool': 'inspect_coverage'})
        assert response.status == 403
        response = await session.post(service.make_url('/v1/ingest'), json={'sources': []})
        assert response.status == 403


async def test_file_revision_rename_delete_and_independent_thread_journal(stack, tmp_path):
    state, index, service, bot, token, cfg = stack
    state.register_thread(8, 7, 'test_thread_8')
    root = tmp_path / 'channels' / 'test_thread_8' / 'journal'
    root.mkdir(parents=True)
    entry = root / 'day.md'
    entry.write_text('Thread journal unique memory', encoding='utf-8')
    exporter = bot.server.app[EXPORTER]
    await exporter.sync(force_files=True)
    thread_scope = resolve_scope(state, {7: {'name': 'test'}}, 8)
    records = index.search('unique memory', thread_scope)
    assert len(records) == 1 and records[0].domain == 'channel:8'
    assert index.search('unique memory', resolve_scope(state, {7: {'name': 'test'}}, 7)) == []
    old = records[0]
    entry.rename(root / 'renamed.md')
    await exporter.sync(force_files=True)
    assert index.read(old.id, thread_scope) is None
    record = index.search('unique memory', thread_scope)[0]
    (root / 'renamed.md').write_text('A corrected journal', encoding='utf-8')
    await exporter.sync(force_files=True)
    assert index.read(record.id, thread_scope).revision != record.revision


async def test_export_replay_failure_does_not_advance_checkpoint(tmp_path, monkeypatch):
    state = StateManager(tmp_path / 'bot.db')
    state.insert_message(101, 7, 1, 42, 'Ada', False, 'old', 1)
    exporter = Exporter(state, lambda: {7: {'name': 'test'}}, None)
    exporter.initialized = True
    exporter.policy = memory_export.digest([memory_export.settings(), {'7': 'test'}])

    async def fail(*args, **kwargs):
        raise TimeoutError()

    exporter.send = fail
    exporter.request = fail
    with pytest.raises(TimeoutError):
        await exporter.sync()
    assert state.memory_get('cursor', 0) == 0


async def test_history_audit_refreshes_edits_removes_missing_and_preserves_inbox(tmp_path, monkeypatch):
    state = StateManager(tmp_path / 'bot.db')
    for id in (100, 150, 400):
        state.insert_message(id, 7, 1, 42, 'Ada', False, 'old', 1)
    state.update_last_seen(7, 100)
    monkeypatch.setattr('wendy.memory_backfill.discord.utils.time_snowflake', lambda *args, **kwargs: 300)

    async def history(**kwargs):
        after = kwargs['after'].id if kwargs['after'] else 0
        for id in (100, 200):
            if id > after:
                yield SimpleNamespace(id=id)

    def cache(message, refresh=False):
        state.insert_message(message.id, 7, 1, 42, 'Ada', False, 'corrected', 1, refresh=refresh)

    await import_channel(SimpleNamespace(id=7, history=history), state,
                         SimpleNamespace(_cache_message=cache), audit=True)
    assert [(r['message_id'], r['content']) for r in state.memory_page()] == [(100, 'corrected'), (200, 'corrected'), (400, 'old')]
    assert state.get_last_seen(7) == 100
    assert state.memory_get('audit:7') is None and state.memory_get('backfill:7') == 200
    assert state.memory_get('history')['7']['audited'] is True
