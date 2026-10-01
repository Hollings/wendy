"""DeepSeek researcher with private tool-loop context and complete token accounting."""
from __future__ import annotations

import json
import os
from pathlib import Path

import aiohttp

from memory_protocol import read_limited

from .gemini_researcher import inline_schema
from .researcher import Answer, ResearchFailure
from .retrieval_mcp import mcp

ENDPOINT = 'https://api.deepseek.com/chat/completions'


def record_usage(target: dict, usage: dict) -> None:
    # completion_tokens already INCLUDES reasoning_tokens. Do not bill them twice.
    prompt = usage.get('prompt_tokens', 0)
    hit = usage.get('prompt_cache_hit_tokens', 0)
    values = {
        'input_tokens': prompt,
        'output_tokens': usage.get('completion_tokens', 0),
        'thinking_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0),
        'cache_read_input_tokens': hit,
        'cache_miss_input_tokens': usage.get('prompt_cache_miss_tokens', prompt - hit),
        'total_tokens': usage.get('total_tokens', prompt + usage.get('completion_tokens', 0)),
        'model_calls': 1,
    }
    for name, value in values.items():
        target[name] = target.get(name, 0) + value


async def run_deepseek(request, gateway, token, url, previous=None):
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        raise ResearchFailure('deepseek_key_missing')
    model = os.getenv('WENDY_MEMORY_DEEPSEEK_MODEL', 'deepseek-flash')
    effort = os.getenv('WENDY_MEMORY_DEEPSEEK_EFFORT', 'low')
    if effort not in ('none', 'low', 'high', 'max'):
        raise ResearchFailure('deepseek_invalid_effort')
    system = (Path(__file__).resolve().parents[2] / 'config' / 'memory_researcher.txt').read_text(encoding='utf-8')
    system += '\nCall finish_research with your final answer. Use no other means of returning it.'
    functions = [{'name': t.name, 'description': t.description, 'parameters': inline_schema(t.inputSchema)}
                 for t in await mcp.list_tools()]
    functions.append({'name': 'finish_research', 'description': 'Return the verified final answer and original citations.',
                      'parameters': inline_schema(Answer.model_json_schema())})
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps({
        'question': request.question, 'context': request.context, 'previous_answer': previous,
        'answer_max_chars': gateway.budget.answer_chars, 'max_citations': gateway.budget.citations,
    })}]
    gateway.usage.update(provider='deepseek', model=model, reasoning_effort=effort)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=gateway.budget.seconds)) as session:
        for _ in range(gateway.max_calls + 2):
            payload = {'model': model, 'messages': messages,
                       'tools': [{'type': 'function', 'function': f} for f in functions],
                       'max_tokens': 5000, 'thinking': {'type': 'disabled' if effort == 'none' else 'enabled'}}
            if effort != 'none':
                payload['reasoning_effort'] = effort
            else:
                payload['temperature'] = 0.2
            async with session.post(ENDPOINT, headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
                if response.status != 200:
                    raise ResearchFailure(f'deepseek_http_{response.status}')
                body = json.loads(await read_limited(response.content, 1_000_000))
            record_usage(gateway.usage, body.get('usage', {}))
            if body.get('model'):
                gateway.usage['response_model'] = body['model']
            choices = body.get('choices', [])
            if not choices or not choices[0].get('message'):
                raise ResearchFailure('deepseek_no_candidate')
            if choices[0].get('finish_reason') == 'length':
                raise ResearchFailure('deepseek_output_limit')
            message = choices[0]['message']
            # Tool calls require previous reasoning_content to be passed back. It lives
            # only in this request's context; never persist or expose it to Wendy.
            messages.append({k: message[k] for k in ('role', 'content', 'reasoning_content', 'tool_calls') if k in message})
            calls = message.get('tool_calls') or []
            if not calls:
                raise ResearchFailure('deepseek_no_tool_call')
            for call in calls:
                function = call['function']
                arguments = json.loads(function['arguments'])
                if not isinstance(arguments, dict):
                    raise ResearchFailure('deepseek_invalid_arguments')
                if function['name'] == 'finish_research':
                    return Answer.model_validate(arguments)
                result = await gateway.call(function['name'], arguments)
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
    raise ResearchFailure('deepseek_turn_limit')
