"""API-native researcher: isolated context, only the same four retrieval tools."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from memory_protocol import read_limited

from .researcher import Answer, ResearchFailure, safe_failure_code, validate
from .retrieval_mcp import mcp


def inline_schema(schema: dict) -> dict:
    definitions = schema.get('$defs', {})

    def expand(value):
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            if '$ref' in value:
                return expand(definitions[value['$ref'].split('/')[-1]])
            return {k: expand(v) for k, v in value.items() if k not in ('$defs', 'title', 'default')}
        return value

    return expand(schema)


async def run_gemini(request, gateway, token, url, previous=None):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise ResearchFailure('gemini_key_missing')
    model = os.getenv('WENDY_MEMORY_GEMINI_MODEL', 'gemini-3.5-flash')
    endpoint = 'https://generativelanguage.googleapis.com/v1beta/models/' + quote(model, safe='-._') + ':generateContent'
    system = (Path(__file__).resolve().parents[2] / 'config' / 'memory_researcher.txt').read_text(encoding='utf-8')
    system += '\nCall finish_research with your final answer. Use no other means of returning it.'
    system += ('\nWhen retrieval is exhausted, finish with the verified evidence already available. '
               'Use no_evidence with empty citations and no citation markers if nothing relevant was found.')
    tools = [{'name': t.name, 'description': t.description, 'parametersJsonSchema': inline_schema(t.inputSchema)}
             for t in await mcp.list_tools()]
    answer_schema = inline_schema(Answer.model_json_schema())
    answer_schema['properties']['answer']['maxLength'] = gateway.budget.answer_chars
    answer_schema['properties']['citations']['maxItems'] = gateway.budget.citations
    tools.append({'name': 'finish_research', 'description': 'Return the verified final answer and original citations.',
                  'parametersJsonSchema': answer_schema})
    contents = [{'role': 'user', 'parts': [{'text': json.dumps({
        'question': request.question, 'context': request.context, 'previous_answer': previous,
        'answer_max_chars': gateway.budget.answer_chars,
        'max_citations': gateway.budget.citations,
    })}]}]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=22)) as session:
        repairs = 0
        for _ in range(gateway.max_calls + 2):
            calling = {'mode': 'ANY'}
            # Reserve the last model turn for an answer instead of repeatedly
            # searching after the gateway's finite retrieval budget is exhausted.
            finish_seconds = min(12, gateway.budget.seconds / 2)
            if gateway.calls >= gateway.max_calls or gateway.remaining <= 0 or gateway.expires - time.monotonic() <= finish_seconds:
                calling['allowedFunctionNames'] = ['finish_research']
            payload = {'systemInstruction': {'parts': [{'text': system}]}, 'contents': contents,
                       'tools': [{'functionDeclarations': tools}],
                       'toolConfig': {'functionCallingConfig': calling},
                       'generationConfig': {'maxOutputTokens': 5000, 'temperature': 0.2}}
            async with session.post(endpoint, headers={'x-goog-api-key': key}, json=payload) as response:
                if response.status != 200:
                    raise ResearchFailure(f'gemini_http_{response.status}')
                raw = await read_limited(response.content, 1_000_000)
                body = json.loads(raw)
            usage = body.get('usageMetadata', {})
            for name, key_name in (('input_tokens', 'promptTokenCount'), ('output_tokens', 'candidatesTokenCount')):
                gateway.usage[name] = gateway.usage.get(name, 0) + usage.get(key_name, 0)
            thinking = usage.get('thoughtsTokenCount', 0)
            gateway.usage['output_tokens'] += thinking
            for name, count in (('thinking_tokens', thinking),
                                ('cache_read_input_tokens', usage.get('cachedContentTokenCount', 0)),
                                ('total_tokens', usage.get('totalTokenCount', 0)), ('model_calls', 1)):
                gateway.usage[name] = gateway.usage.get(name, 0) + count
            gateway.usage.update(provider='gemini', model=model)
            candidates = body.get('candidates', [])
            if not candidates or 'content' not in candidates[0]:
                raise ResearchFailure('gemini_no_candidate')
            content = candidates[0]['content']
            # Preserve opaque thought signatures exactly for subsequent API turns.
            # They are never logged, saved to the source corpus, or returned to Wendy.
            contents.append(content)
            responses = []
            for part in content.get('parts', []):
                if call := part.get('functionCall'):
                    if call['name'] == 'finish_research':
                        try:
                            answer = Answer.model_validate(call.get('args', {}))
                            validate(answer, gateway, request)
                        except ValueError as exc:
                            repairs += 1
                            if repairs > 2:
                                raise ResearchFailure('answer_repair_limit') from None
                            result = {'error': safe_failure_code(exc), 'instruction': (
                                'Correct finish_research. Respect the answer/citation limits; cite only verified '
                                'originals with exact excerpts of at most 400 characters. Each [S1] marker must '
                                'match a citation. If no relevant evidence was found, use no_evidence with '
                                'empty citations and no citation markers.')}
                        else:
                            return answer
                    else:
                        result = await gateway.call(call['name'], call.get('args', {}))
                        result['research_budget'] = {'remaining_calls': max(0, gateway.max_calls - gateway.calls),
                            'remaining_seconds': max(0, round(gateway.expires - time.monotonic(), 1)),
                            'instruction': 'Finish with available verified evidence before this budget expires.'}
                    response = {'name': call['name'], 'response': result}
                    if call.get('id'):
                        response['id'] = call['id']
                    responses.append({'functionResponse': response})
            if not responses:
                raise ResearchFailure('gemini_no_tool_call')
            contents.append({'role': 'user', 'parts': responses})
    raise ResearchFailure('gemini_turn_limit')
