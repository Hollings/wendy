"""Private HTTP service. Public-facing controllers only access the bot gateway."""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from memory_protocol import EvidenceRequest, ResearchRequest, Scope, Source

from .hindsight import Hindsight
from .index import Index
from .researcher import Researcher

INDEX = web.AppKey('index', Index)
RESEARCHER = web.AppKey('researcher', Researcher)


def create_app(index: Index, backend=None, runner=None) -> web.Application:
    key = os.environ.get('WENDY_MEMORY_SERVICE_TOKEN', '')
    if len(key) < 32:
        raise ValueError('WENDY_MEMORY_SERVICE_TOKEN must have at least 32 characters')

    @web.middleware
    async def boundary(request, handler):
        if request.path not in ('/health', '/v1/retrieve') and not hmac.compare_digest(
            request.headers.get('Authorization', ''), 'Bearer ' + key
        ):
            raise web.HTTPForbidden()
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError):
            return web.json_response({'error': 'Invalid memory request'}, status=400)

    app = web.Application(middlewares=[boundary], client_max_size=2_000_000)
    app[INDEX] = index

    async def lifetime(app):
        async with aiohttp.ClientSession() as session:
            adapter = backend or Hindsight(session, os.getenv('HINDSIGHT_URL', 'http://hindsight:8888'),
                                            os.getenv('HINDSIGHT_API_KEY', ''),
                                            os.getenv('HINDSIGHT_BANK_PREFIX', 'wendy-v1'))
            app[RESEARCHER] = Researcher(index, adapter,
                os.getenv('MEMORY_GATEWAY_URL', 'http://127.0.0.1:8950/v1/retrieve'),
                **({'runner': runner} if runner else {}))
            task = asyncio.create_task(adapter.run(index))
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(lifetime)

    async def health(request):
        return web.json_response({'status': 'ok', 'source_revision': index.get_meta('revision', 0)})

    async def status(request):
        return web.json_response({'storage_id': index.get_meta('storage_id')})

    async def ingest(request):
        body = await request.json()
        sources = [Source.model_validate(s) for s in body.get('sources', [])]
        if len(sources) > 200 or len(body.get('deleted', [])) > 200:
            raise ValueError('batch limit')
        index.ingest(sources, body.get('deleted', []), body.get('epoch'))
        if sweep := body.get('sweep'):
            index.sweep(sweep['epoch'], sweep['kinds'])
        if coverage := body.get('coverage'):
            with index.db:
                index.set_meta('sync', {k: v for k, v in coverage.items() if k != 'history'})
                index.set_meta('history', coverage.get('history', {}))
        return web.json_response({'ok': True, 'source_revision': index.get_meta('revision')})

    async def research(request):
        body = await request.json()
        scope = Scope.model_validate(body['scope'])
        return web.json_response(await app[RESEARCHER].research(ResearchRequest.model_validate(body['request']),
                                                               scope, body.get('mode', 'combined')))

    async def retrieve(request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        gateway = app[RESEARCHER].capabilities.get(token)
        if not gateway or time.monotonic() >= gateway.expires:
            raise web.HTTPForbidden()
        body = await request.json()
        return web.json_response(await gateway.call(body['tool'], body.get('arguments', {})))

    async def evidence(request):
        body = await request.json()
        scope = Scope.model_validate(body['scope'])
        query = EvidenceRequest.model_validate(body['request'])
        previous = index.research(query.research_id, scope)
        if not previous:
            return web.json_response({'error': 'Evidence expired, changed, or is outside this conversation'}, status=404)
        sources, more = [], False
        for citation in previous['sources']:
            if citation['ref'] not in query.refs:
                continue
            source = index.read(citation['source_id'], scope)
            text = source.text[query.cursor:query.cursor + 1500]
            sources.append({**citation, 'text': text})
            more |= len(source.text) > query.cursor + 1500
        return web.json_response({'research_id': query.research_id, 'sources': sources,
                                  'next_cursor': query.cursor + 1500 if more else None})

    async def validate_result(request):
        body = await request.json()
        scope = Scope.model_validate(body['scope'])
        result = index.research(body['research_id'], scope)
        if result is None:
            raise web.HTTPNotFound()
        return web.json_response(result)

    app.router.add_get('/health', health)
    app.router.add_post('/v1/status', status)
    app.router.add_post('/v1/ingest', ingest)
    app.router.add_post('/v1/research', research)
    app.router.add_post('/v1/retrieve', retrieve)
    app.router.add_post('/v1/evidence', evidence)
    app.router.add_post('/v1/validate', validate_result)
    return app


if __name__ == '__main__':
    index = Index(Path(os.getenv('WENDY_MEMORY_DB', '/data/memory/source.db')))
    web.run_app(create_app(index), host=os.getenv('WENDY_MEMORY_BIND', '127.0.0.1'),
                port=int(os.getenv('WENDY_MEMORY_PORT', '8950')), access_log=None, handler_cancellation=True)
