"""Idle, reusable Claude processes. Durable conversation state stays on disk."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from . import task_auth
from .worker_runtime import stop_process

_LOG = logging.getLogger(__name__)


@dataclass
class Client:
    process: asyncio.subprocess.Process
    token: str
    scope: dict
    signature: tuple
    session_id: str
    busy: bool = True
    last_used: float = 0
    expiry: asyncio.TimerHandle | None = None


class ClientPool:
    def __init__(self):
        self.clients: dict[int, Client] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    async def _discard(self, channel_id: int, client: Client):
        if self.clients.get(channel_id) is client:
            self.clients.pop(channel_id)
        if client.expiry:
            client.expiry.cancel()
        task_auth.revoke(client.token)
        # Kill the entire process group, including any tool children.
        await stop_process(client.process)

    async def _expire(self, channel_id: int, client: Client):
        if self.clients.get(channel_id) is client and not client.busy:
            await self._discard(channel_id, client)

    async def acquire(self, channel_id: int, session_id: str, signature: tuple, launch):
        lock = self.locks.setdefault(channel_id, asyncio.Lock())
        await lock.acquire()
        try:
            client = self.clients.get(channel_id)
            if client and (client.signature != signature or client.session_id != session_id
                           or client.process.returncode is not None):
                reason = ('process exited' if client.process.returncode is not None else
                          'session changed' if client.session_id != session_id else 'prompt or configuration changed')
                _LOG.info('Client replacement channel=%s reason=%s', channel_id, reason)
                await self._discard(channel_id, client)
                client = None
            reused = client is not None
            if client is None:
                client = await launch()
                self.clients[channel_id] = client
            if client.expiry:
                client.expiry.cancel()
            client.busy = True
            client.scope['active'] = True
            client.scope.pop('delivery_notice', None)
            client.scope.pop('automatic_delivery', None)
            return client, reused
        except BaseException:
            lock.release()
            raise

    async def release(self, channel_id: int, client: Client, *, healthy: bool, keep_warm: bool):
        try:
            client.busy = False
            client.scope['active'] = False  # Idle CLI cannot use a controller capability.
            client.session_id = client.scope.get('session_id') or client.session_id
            client.last_used = time.monotonic()
            ttl = max(0, int(os.getenv('WENDY_CLIENT_IDLE_SECONDS', '600')))
            limit = max(0, int(os.getenv('WENDY_WARM_CLIENT_LIMIT', '4')))
            if not healthy or not keep_warm or not ttl or not limit or client.process.returncode is not None:
                await self._discard(channel_id, client)
                return
            idle = sorted(((cid, item) for cid, item in self.clients.items() if not item.busy),
                          key=lambda pair: pair[1].last_used)
            # The cap covers retained idle clients; active conversations are never evicted.
            for cid, item in idle[:-limit]:
                await self._discard(cid, item)
            if self.clients.get(channel_id) is client:
                client.expiry = asyncio.get_running_loop().call_later(
                    ttl, lambda: asyncio.create_task(self._expire(channel_id, client)))
        finally:
            self.locks[channel_id].release()

    async def close(self):
        await asyncio.gather(*(self._discard(cid, client) for cid, client in list(self.clients.items())),
                             return_exceptions=True)

    async def close_channel(self, channel_id: int):
        async with self.locks.setdefault(channel_id, asyncio.Lock()):
            client = self.clients.get(channel_id)
            if client:
                await self._discard(channel_id, client)


clients = ClientPool()
