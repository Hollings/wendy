"""Authenticated bot gateway; scope and delivery cutoffs never come from Wendy."""
from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from memory_protocol import EvidenceRequest, ResearchRequest

from . import task_auth
from .memory_export import Exporter, enabled, resolve_scope

EXPORTER = web.AppKey('memory_exporter', Exporter)


def install(app: web.Application, state_provider, configs_provider):
    async def lifetime(app):
        if not enabled():
            yield
            return
        async with aiohttp.ClientSession() as session:
            exporter = Exporter(state_provider(), configs_provider, session)
            app[EXPORTER] = exporter
            task = asyncio.create_task(exporter.run())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def handle(request: web.Request):
        capability = task_auth.lookup(request.headers.get('Authorization', '').removeprefix('Bearer '))
        if not capability or capability.get('role') != 'controller':
            raise web.HTTPForbidden()
        exporter = request.app.get(EXPORTER)
        if not exporter:
            return web.json_response({'error': 'Historical memory service is not enabled'}, status=503)
        try:
            body = await request.json()
            evidence = request.path.endswith('/evidence')
            query = EvidenceRequest.model_validate(body) if evidence else ResearchRequest.model_validate(body)
            scope = resolve_scope(state_provider(), configs_provider(), capability['channel_id'])
            await exporter.sync(force_files=True)
            endpoint = '/v1/evidence' if evidence else '/v1/research'
            result = await exporter.request(endpoint, {'request': query.model_dump(), 'scope': scope.model_dump()})
            # Recheck after a long run: edits, deletion and policy changes cannot
            # escape in the final answer or evidence page.
            if not task_auth.lookup(request.headers.get('Authorization', '').removeprefix('Bearer ')):
                raise web.HTTPForbidden()
            await exporter.sync(force_files=True)
            current = resolve_scope(state_provider(), configs_provider(), capability['channel_id'])
            if current.policy != scope.policy:
                return web.json_response({'error': 'Memory policy changed; retry the question'}, status=409)
            if not evidence and result['status'] == 'unavailable':
                return web.json_response(result)
            await exporter.request('/v1/validate', {'research_id': result['research_id'], 'scope': current.model_dump()})
            return web.json_response(result)
        except PermissionError:
            raise web.HTTPForbidden() from None
        except (ValueError, TypeError, KeyError):
            return web.json_response({'error': 'Invalid memory request or configuration'}, status=400)
        except (aiohttp.ClientError, OSError, TimeoutError):
            return web.json_response({'error': 'Memory is unavailable or its evidence changed; retry later'}, status=503)

    app.cleanup_ctx.append(lifetime)
    app.router.add_post('/api/memory/research', handle)
    app.router.add_post('/api/memory/evidence', handle)
