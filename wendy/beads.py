"""Typed failure boundary around the pinned external BD CLI."""
from __future__ import annotations

import asyncio
import json
import os

from .config import CLI_SUBPROCESS_UID, SENSITIVE_ENV_VARS
from .paths import beads_dir, channel_dir
from .worker_runtime import stop_process


class BeadsError(RuntimeError):
    pass


class BeadsClient:
    def __init__(self, binary: str | None = None):
        # Never resolve the public `bd` shim: it only redirects agents to wtask.
        self.binary = binary or os.getenv('WENDY_BD_BINARY', '/usr/local/libexec/wendy-bd')

    async def run(self, queue: str, *args: str):
        env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
        env['BEADS_DIR'] = str(beads_dir(queue))
        if CLI_SUBPROCESS_UID is not None:
            env['HOME'] = '/home/wendy'
        kwargs = {'user': CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID is not None else {}
        proc = await asyncio.create_subprocess_exec(
            self.binary, *args, cwd=channel_dir(queue), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == 'posix', **kwargs)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except BaseException:
            # npm's bd entry point launches a native child. Terminating only
            # the wrapper leaves that child holding the embedded database lock.
            await stop_process(proc)
            raise
        if proc.returncode:
            raise BeadsError(stderr.decode(errors='replace').strip() or f'bd exited {proc.returncode}')
        return stdout.decode(errors='replace')

    async def json(self, queue: str, *args: str):
        output = await self.run(queue, *args, '--json')
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise BeadsError('BD returned invalid JSON; task state was not changed') from exc

    async def ready(self, queue: str) -> list[dict]:
        result = await self.json(queue, 'ready', '--sort', 'priority', '--limit', '0')
        if not isinstance(result, list) or any(not isinstance(r, dict) or not r.get('id') for r in result):
            raise BeadsError('Unexpected bd ready response')
        return result

    async def show(self, queue: str, task_id: str) -> dict:
        result = await self.json(queue, 'show', task_id)
        if isinstance(result, list):
            result = result[0] if result else None
        if not isinstance(result, dict) or not result.get('id'):
            raise BeadsError('Unexpected bd show response')
        return result

    async def update(self, queue: str, task_id: str, status: str, reason: str = ''):
        if status == 'closed':
            await self.run(queue, 'close', task_id, '-r', reason or 'Worker report ready for review')
        else:
            await self.run(queue, 'update', task_id, '--status', status, '--assignee', 'wendy-controller')
