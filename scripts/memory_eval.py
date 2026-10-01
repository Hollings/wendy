"""Run an administrator-owned memory evaluation suite against the private service.

Each JSONL case contains id, question, scope (the trusted Scope wire contract),
expected_source_ids, optional forbidden_terms and should_abstain. Output contains
private research answers: keep it outside Git. No messages are sent to Discord.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from pathlib import Path

import aiohttp

from memory_protocol import render


async def evaluate(args):
    cases = [json.loads(line) for line in args.suite.read_text(encoding='utf-8').splitlines() if line.strip()]
    results = []
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + os.environ['WENDY_MEMORY_SERVICE_TOKEN']},
                                     timeout=aiohttp.ClientTimeout(total=75)) as session:
        for mode in args.modes:
            for case in cases:
                async with session.post(args.url.rstrip('/') + '/v1/research', json={
                    'request': {'question': case['question'], 'depth': args.depth},
                    'scope': case['scope'], 'mode': mode,
                }) as response:
                    response.raise_for_status()
                    result = await response.json()
                cited = {s['source_id'] for s in result['sources']}
                expected = set(case.get('expected_source_ids', []))
                results.append({'id': case['id'], 'mode': mode, 'result': result,
                    'evidence_recall': len(cited & expected) / len(expected) if expected else None,
                    'forbidden_term_seen': any(t.casefold() in render(result).casefold() for t in case.get('forbidden_terms', [])),
                    'abstained': result['status'] in ('no_evidence', 'unavailable'),
                    'should_abstain': case.get('should_abstain', False),
                    'wendy_response_characters': len(render(result)),
                    'human_grounding_review': 'pending'})
                args.out.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in results) + '\n', encoding='utf-8')
    for mode in args.modes:
        rows = [r for r in results if r['mode'] == mode]
        latencies = [r['result'].get('metrics', {}).get('seconds', 0) for r in rows]
        print(json.dumps({'mode': mode, 'queries': len(rows), 'median_seconds': statistics.median(latencies),
                          'forbidden_term_failures': sum(r['forbidden_term_seen'] for r in rows),
                          'human_grounding_review': 'required'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--url', default=os.getenv('WENDY_MEMORY_URL', 'http://127.0.0.1:8950'))
    parser.add_argument('--modes', nargs='+', choices=['sources', 'hindsight', 'combined'], default=['sources', 'hindsight', 'combined'])
    parser.add_argument('--depth', choices=['standard', 'deep'], default='standard')
    asyncio.run(evaluate(parser.parse_args()))


if __name__ == '__main__':
    main()
