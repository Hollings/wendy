"""Per-research retrieval capability and limits; never accepts scope from the LLM."""
from __future__ import annotations

import json
import time

from memory_protocol import Scope, Source, budget

from .index import Index


class Gateway:
    def __init__(self, index: Index, backend, scope: Scope, depth: str, mode='combined'):
        if mode not in ('sources', 'hindsight', 'combined'):
            raise ValueError('Unknown retrieval mode')
        self.mode = mode
        self.index, self.backend, self.scope = index, backend, scope
        self.budget = budget(depth)
        self.calls = 0
        self.max_calls = self.budget.calls
        self.remaining = self.budget.source_bytes
        self.expires = time.monotonic() + self.budget.seconds
        self.seen: dict[str, tuple[str, list[str]]] = {}
        self.events = []
        self.backend_unavailable = False
        self.usage = {}

    def source(self, source: Source, offset=0) -> dict:
        text = source.text[offset:offset + 4000]
        return {**source.model_dump(exclude={'text'}), 'text': text, 'revision': source.revision,
                'next_offset': offset + 4000 if len(source.text) > offset + 4000 else None}

    async def call(self, tool: str, args: dict) -> dict:
        if time.monotonic() >= self.expires or self.calls >= self.max_calls or self.remaining <= 0:
            return {'error': 'Research budget exhausted. Finish with the verified evidence already retrieved.'}
        self.calls += 1
        if (tool == 'recall_memory' and self.mode == 'sources') or (tool == 'search_sources' and self.mode == 'hindsight'):
            return {'disabled': f'{tool} is disabled for the {self.mode} evaluation mode; use the other retrieval route.'}
        if tool == 'inspect_coverage':
            result = {**self.index.coverage(self.scope), 'retrieval_mode': self.mode}
        elif tool == 'search_sources':
            query = str(args.get('query', ''))[:2000]
            records = self.index.search(query, self.scope, limit=5, kind=args.get('kind'),
                                        after=args.get('after'), before=args.get('before'))
            result = {'sources': [self.source(s) for s in records]}
        elif tool == 'read_sources':
            ids = args.get('source_ids', [])[:4]
            offset = max(0, min(int(args.get('offset', 0)), 500000))
            sources = {}
            for source_id in ids:
                source = self.index.read(source_id, self.scope)
                if source:
                    sources[source.id] = source
                    if args.get('neighbors', True):
                        sources.update({s.id: s for s in self.index.neighbors(source, self.scope, count=1)})
            result = {'sources': [self.source(s, offset if s.id in ids else 0) for s in sources.values()]}
        elif tool == 'recall_memory':
            result = await self.backend.recall(self.index, self.scope, str(args.get('query', ''))[:2000])
            self.backend_unavailable |= result.get('unavailable', False)
        else:
            raise ValueError('Unknown retrieval tool')
        # Bound complete JSON rather than returning a broken/truncated JSON string.
        # Coverage's `sources` is a kind -> count mapping, not source records.
        key = 'sources' if isinstance(result.get('sources'), list) else 'leads' if 'leads' in result else None
        cap = min(self.remaining, 24000)
        while key and result[key] and len(json.dumps(result).encode()) > cap:
            result[key].pop()
            result['truncated'] = True
        size = len(json.dumps(result).encode())
        if size > cap:
            result = {'error': 'Source budget exhausted'}
            size = len(json.dumps(result).encode())
        self.remaining -= size
        # Only receipt snippets actually delivered may be cited.
        records = result.get('sources', []) if key == 'sources' else []
        delivered = {s['id'] for s in records}
        for source in records:
            revision, chunks = self.seen.get(source['id'], (source['revision'], []))
            self.seen[source['id']] = (source['revision'],
                (chunks if revision == source['revision'] else []) + [source['text']])
        self.events.append({'tool': tool, 'source_ids': list(delivered), 'bytes': size})
        return result
