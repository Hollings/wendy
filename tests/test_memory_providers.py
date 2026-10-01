"""Provider wire contracts, billing counters, and failure containment."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from memory_protocol import ResearchRequest, Scope, Source
from services.memory import deepseek_researcher as deepseek
from services.memory import gemini_researcher as gemini
from services.memory.index import Index
from services.memory.researcher import Researcher
from wendy.config import SENSITIVE_ENV_VARS


class Backend:
    async def recall(self, *args):
        return {'leads': [], 'unavailable': False}


@pytest.mark.parametrize('failure', [None, 'http', 'length', 'forged', 'arguments', 'no_tools'])
async def test_deepseek_tool_loop(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-secret')
    monkeypatch.setenv('WENDY_MEMORY_RESEARCHER', 'deepseek')
    monkeypatch.setenv('WENDY_MEMORY_DEEPSEEK_EFFORT', 'low')
    record = Source(id='discord:1', domain='channel:7', kind='chat', text='Ada chose Postgres.',
                    speaker='Ada', timestamp=1, channel_id='7', message_id='1', location='test', locator='test')
    index = Index(tmp_path / 'index.db')
    index.ingest([record], [])
    scope = Scope(origin='7', domains=['channel:7'], cutoffs={'7': '1'}, policy='test')
    requests = []

    async def provider(request):
        assert request.headers['Authorization'] == 'Bearer test-secret'
        body = await request.json()
        requests.append(body)
        assert body['thinking'] == {'type': 'enabled'} and body['reasoning_effort'] == 'low'
        if failure == 'http':
            return web.Response(status=401, text='secret error body must not be logged')
        if len(requests) == 1:
            name, args = 'search_sources', {'query': 'Postgres'}
        else:
            assistant = body['messages'][2]
            assert assistant['reasoning_content'] == 'private reasoning marker'
            assert body['messages'][3]['tool_call_id'] == 'call-1'
            original = json.loads(body['messages'][3]['content'])['sources'][0]
            name, args = 'finish_research', {'status': 'answered', 'answer': 'Ada chose Postgres. [S1]',
                'citations': [{'ref': 'S1', 'source_id': original['id'], 'revision': original['revision'],
                               'excerpt': 'invented quotation' if failure == 'forged' else original['text']}],
                'limitations': []}
        message = {'role': 'assistant', 'content': None, 'reasoning_content': 'private reasoning marker',
                   'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
                       'name': name, 'arguments': '[]' if failure == 'arguments' else json.dumps(args)}}]}
        if failure == 'no_tools':
            message['tool_calls'] = []
        return web.json_response({'choices': [{'message': message, 'finish_reason': 'length' if failure == 'length' else 'tool_calls'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
                      'prompt_cache_hit_tokens': 60, 'prompt_cache_miss_tokens': 40,
                      'completion_tokens_details': {'reasoning_tokens': 30}}})

    app = web.Application()
    app.router.add_post('/chat/completions', provider)
    async with TestServer(app) as server:
        monkeypatch.setattr(deepseek, 'ENDPOINT', str(server.make_url('/chat/completions')))
        try:
            result = await Researcher(index, Backend(), 'unused').research(ResearchRequest(question='Database?'), scope)
        finally:
            index.db.close()
    serialized = json.dumps(result)
    assert 'private reasoning marker' not in serialized and 'test-secret' not in serialized
    assert 'secret error body' not in serialized
    if failure:
        assert result['status'] == 'unavailable' and not result['sources']
    else:
        assert result['status'] == 'answered' and len(result['sources']) == 1
        usage = result['metrics']['usage']
        assert usage['input_tokens'] == 200 and usage['output_tokens'] == 80
        assert usage['thinking_tokens'] == 60 and usage['total_tokens'] == 280
        assert usage['cache_read_input_tokens'] == 120 and usage['cache_miss_input_tokens'] == 80
        assert usage['model_calls'] == 2
    assert 'DEEPSEEK_API_KEY' in SENSITIVE_ENV_VARS


async def test_gemini_billing_includes_thinking_once(tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-secret')
    monkeypatch.setenv('WENDY_MEMORY_RESEARCHER', 'gemini')
    answer = {'status': 'no_evidence', 'answer': 'No evidence.', 'citations': [], 'limitations': []}

    async def provider(request):
        return web.json_response({'candidates': [{'content': {'role': 'model', 'parts': [
            {'functionCall': {'name': 'finish_research', 'args': answer}}]}}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 40,
                              'thoughtsTokenCount': 30, 'totalTokenCount': 170, 'cachedContentTokenCount': 60}})

    app = web.Application()
    app.router.add_post('/generate', provider)
    original_post = gemini.aiohttp.ClientSession.post
    async with TestServer(app) as server:
        def local_post(session, url, **kwargs):
            return original_post(session, server.make_url('/generate'), **kwargs)

        monkeypatch.setattr(gemini.aiohttp.ClientSession, 'post', local_post)
        index = Index(tmp_path / 'index.db')
        scope = Scope(origin='7', domains=['channel:7'], cutoffs={'7': '1'}, policy='test')
        try:
            result = await Researcher(index, Backend(), 'unused').research(ResearchRequest(question='Database?'), scope)
        finally:
            index.db.close()
    usage = result['metrics']['usage']
    assert result['status'] == 'no_evidence'
    assert usage['input_tokens'] == 100 and usage['output_tokens'] == 70
    assert usage['thinking_tokens'] == 30 and usage['total_tokens'] == 170
    assert usage['cache_read_input_tokens'] == 60
