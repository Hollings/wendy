"""Opt-in live test with synthetic history, real Hindsight, and the real researcher.

Run: python -m scripts.memory_smoke --env-file .env --docker
Uses provider credentials; prints no credentials or real history.
Creates and removes one uniquely named disposable Hindsight container. Does not
connect to Discord or alter Wendy's runtime. --docker requires a pulled 0.10.2 image.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestServer
from dotenv import dotenv_values

from memory_protocol import ResearchRequest, Scope, Source
from services.memory.hindsight import Hindsight
from services.memory.index import Index
from services.memory.server import RESEARCHER, create_app


async def exercise(url: str, repeats=1, extended=False, out=None):
    with tempfile.TemporaryDirectory(prefix='memory-smoke-') as directory:
        index = Index(Path(directory) / 'memory.db')
        sources = [Source(id=f'discord:{100 + i}', domain='channel:7', kind='chat', text=text,
                          speaker=speaker, timestamp=1700000000 + i, channel_id='7', message_id=str(100 + i),
                          location='synthetic test', locator=f'https://discord.com/channels/1/7/{100+i}')
                   for i, (speaker, text) in enumerate([
                       ('Ada', 'For Project Kingfisher I propose MySQL.'),
                       ('Bo', 'We need Postgres because of JSONB and our existing backups.'),
                       ('Ada', 'Agreed. The final decision for Project Kingfisher is Postgres, not MySQL.'),
                   ])]
        index.ingest(sources, [])
        scope = Scope(origin='7', domains=['channel:7'], cutoffs={'7': '102'}, policy='smoke')
        async with aiohttp.ClientSession() as session:
            backend = Hindsight(session, url, os.getenv('HINDSIGHT_API_KEY', ''), 'smoke-' + uuid.uuid4().hex[:8])
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                try:
                    async with session.get(url + '/health', timeout=aiohttp.ClientTimeout(total=3)) as response:
                        if response.status == 200:
                            break
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(3)
            else:
                raise RuntimeError('Hindsight failed to become ready')
            print('Hindsight ready', flush=True)
            server = TestServer(create_app(index, backend))
            await server.start_server()
            server.app[RESEARCHER].gateway_url = str(server.make_url('/v1/retrieve'))
            try:
                deadline = time.monotonic() + 240
                while index.coverage(scope)['pending_documents'] and time.monotonic() < deadline:
                    await asyncio.sleep(2)
                if index.coverage(scope)['pending_documents']:
                    raise RuntimeError('Hindsight ingestion did not complete')
                recall = await backend.recall(index, scope, 'What database did Kingfisher choose and why?')
                assert recall['leads'] and not recall['unavailable'], 'No grounded Hindsight recall'
                print('Hindsight retain and grounded recall passed', flush=True)
                cases = [(f'decision-{i + 1}',
                          'What database did Project Kingfisher finally choose and why? Check Hindsight and original sources.',
                          scope) for i in range(repeats)]
                if extended:
                    cases.extend([
                        ('missing', 'What launch date was finally agreed for Project Kingfisher? Check both retrieval routes.', scope),
                        ('cutoff', 'What database proposal for Project Kingfisher can you find in the available records?',
                         scope.model_copy(update={'cutoffs': {'7': '100'}})),
                    ])
                for case, question, case_scope in cases:
                    result = await server.app[RESEARCHER].research(ResearchRequest(question=question, depth='deep'), case_scope)
                    report = {'case': case, **result}
                    print(json.dumps(report, indent=2), flush=True)
                    if out:
                        with out.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps(report) + '\n')
                    assert result['status'] != 'unavailable', case
                    if case.startswith('decision-'):
                        assert result['status'] in ('answered', 'partial') and result['sources']
                        assert 'postgres' in result['answer'].lower()
                        assert 'jsonb' in result['answer'].lower() and 'backup' in result['answer'].lower()
                        assert 'discord:102' in {s['source_id'] for s in result['sources']}
                        assert any(e['tool'] == 'recall_memory' for e in result['metrics']['tool_events'])
                    elif case == 'missing':
                        assert result['status'] == 'no_evidence'
                        assert {'recall_memory', 'search_sources'} <= {e['tool'] for e in result['metrics']['tool_events']}
                    else:
                        assert 'mysql' in result['answer'].lower() and 'postgres' not in result['answer'].lower()
                        assert {s['source_id'] for s in result['sources']} == {'discord:100'}
                # Correct/delete the whole episode and verify no old fact can escape.
                index.ingest([], [s.id for s in sources])
                assert not (await backend.recall(index, scope, 'Kingfisher database'))['leads']
                deadline = time.monotonic() + 60
                while index.coverage(scope)['pending_documents'] and time.monotonic() < deadline:
                    await asyncio.sleep(2)
                assert not index.coverage(scope)['pending_documents']
                raw = await backend.request('POST', 'channel:7', '/memories/recall',
                                            {'query': 'Kingfisher', 'types': ['world', 'experience']})
                assert not raw['results'], 'Backend deletion did not remove facts'
                print('Revision suppression and remote deletion passed', flush=True)
            finally:
                await server.close()
                index.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--docker', action='store_true')
    parser.add_argument('--research-image', help='Run the test in this built memory image (production Linux runtime)')
    parser.add_argument('--researcher', choices=['claude', 'gemini', 'deepseek'], default='gemini')
    parser.add_argument('--hindsight-provider', choices=['gemini', 'deepseek'], default='gemini')
    parser.add_argument('--repeats', type=int, choices=range(1, 6), default=1)
    parser.add_argument('--extended', action='store_true', help='Also check missing evidence and a restricted history cutoff')
    parser.add_argument('--out', type=Path, help='Append synthetic results and token counts as JSONL')
    parser.add_argument('--url', default='http://127.0.0.1:18988')
    args = parser.parse_args()
    if args.env_file:
        values = dotenv_values(args.env_file)
        for key in ('GEMINI_API_KEY', 'DEEPSEEK_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY',
                    'WENDY_MEMORY_MODEL', 'WENDY_MEMORY_GEMINI_MODEL',
                    'WENDY_MEMORY_DEEPSEEK_MODEL', 'WENDY_MEMORY_DEEPSEEK_EFFORT'):
            if values.get(key):
                os.environ.setdefault(key, values[key])
    os.environ.setdefault('WENDY_MEMORY_SERVICE_TOKEN', uuid.uuid4().hex + uuid.uuid4().hex)
    os.environ['WENDY_MEMORY_RESEARCHER'] = args.researcher
    name = 'wendy-memory-smoke-' + uuid.uuid4().hex[:10]
    created = False
    try:
        if args.docker:
            defaults = {'gemini': ('GEMINI_API_KEY', 'gemini-3.5-flash'), 'deepseek': ('DEEPSEEK_API_KEY', 'deepseek-flash')}
            key_name, model = defaults[args.hindsight_provider]
            env = {**os.environ, 'HINDSIGHT_API_LLM_PROVIDER': args.hindsight_provider,
                   'HINDSIGHT_API_LLM_API_KEY': os.environ[key_name],
                   'HINDSIGHT_API_LLM_MODEL': os.getenv('HINDSIGHT_API_LLM_MODEL', model),
                   'HINDSIGHT_API_WORKER_ID': name}
            subprocess.run(['docker', 'run', '-d', '--name', name, '--shm-size=1g',
                '-p', '127.0.0.1:18988:8888', '-e', 'HINDSIGHT_API_LLM_PROVIDER',
                '-e', 'HINDSIGHT_API_LLM_API_KEY', '-e', 'HINDSIGHT_API_LLM_MODEL',
                '-e', 'HINDSIGHT_API_WORKER_ID', 'ghcr.io/vectorize-io/hindsight:0.10.2'],
                env=env, check=True, stdout=subprocess.DEVNULL)
            created = True
        if args.research_image:
            mount = f'{Path(__file__).resolve()}:/app/scripts/memory_smoke.py:ro'
            extra = ['--extended'] if args.extended else []
            mounts = []
            if args.out:
                args.out.touch(exist_ok=True)
                mounts = ['-v', f'{args.out.resolve()}:/tmp/memory-smoke-results.jsonl']
                extra += ['--out', '/tmp/memory-smoke-results.jsonl']
            subprocess.run(['docker', 'run', '--rm', '--init', '-v', mount,
                            '-e', 'CLAUDE_CODE_OAUTH_TOKEN', '-e', 'ANTHROPIC_API_KEY',
                            '-e', 'GEMINI_API_KEY', '-e', 'DEEPSEEK_API_KEY',
                            '-e', 'WENDY_MEMORY_DEEPSEEK_MODEL', '-e', 'WENDY_MEMORY_DEEPSEEK_EFFORT',
                            '-e', 'WENDY_MEMORY_GEMINI_MODEL',
                            '-e', 'WENDY_MEMORY_SERVICE_TOKEN', *mounts, args.research_image,
                            'python', '-m', 'scripts.memory_smoke', '--url',
                            'http://host.docker.internal:18988' if args.docker else args.url,
                            '--researcher', args.researcher, '--repeats', str(args.repeats), *extra], check=True)
        else:
            asyncio.run(exercise(args.url, args.repeats, args.extended, args.out))
    finally:
        if created:
            subprocess.run(['docker', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
