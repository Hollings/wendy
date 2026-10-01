"""Bot-owned memory policy and resumable export of original records over HTTP."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp

from memory_protocol import Scope, Source, digest

from .paths import WENDY_BASE, journal_dir

_LOG = logging.getLogger(__name__)
CONFIG = Path(__file__).resolve().parents[1] / 'config' / 'memory.json'


def settings() -> dict:
    return json.loads(Path(os.getenv('WENDY_MEMORY_CONFIG', str(CONFIG))).read_text(encoding='utf-8'))


def enabled() -> bool:
    return os.getenv('WENDY_MEMORY_ENABLED', '').lower() in ('1', 'true', 'yes')


def conversations(state, configs: dict, cfg: dict) -> dict[str, str]:
    selected = {str(i) for i in cfg.get('channels', [])}
    channels = {str(i): c.get('_folder') or c['name'] for i, c in configs.items()
                if not selected or str(i) in selected}
    for thread in state.memory_threads():
        if str(thread['parent_channel_id']) in channels:
            channels[str(thread['thread_id'])] = thread['folder_name']
    return channels


def resolve_scope(state, configs: dict, channel_id: int) -> Scope:
    cfg = settings()
    channels = conversations(state, configs, cfg)
    origin = str(channel_id)
    if origin not in channels:
        raise PermissionError('Memory is not enabled for this conversation')
    ids = list(channels) if cfg.get('cross_channel', False) else [origin]
    domains = ['channel:' + i for i in ids]
    if cfg.get('include_profiles', False):
        domains.append('people')
    return Scope(origin=origin, domains=domains,
                 cutoffs={i: str((state.get_last_seen(int(i)) or 0) if i == origin
                                else state.memory_latest_id(int(i))) for i in ids},
                 policy=digest({'domains': sorted(domains), 'config': cfg}))


def message_source(row: dict, channels: dict[str, str]) -> Source | None:
    channel = str(row['channel_id'])
    if channel not in channels or int(row['message_id']) >= 9_000_000_000_000_000_000:
        return None
    message = str(row['message_id'])
    text = row['content'] or ''
    if row.get('attachment_urls'):
        text += '\nAttachment references (contents not indexed): ' + row['attachment_urls']
    return Source(id='discord:' + message, domain='channel:' + channel, kind='chat', text=text,
                  speaker=row['author_nickname'] or ('Bot' if row['is_bot'] else 'Unknown'),
                  speaker_role='webhook' if row.get('is_webhook') else 'bot' if row['is_bot'] else 'human',
                  author_id=str(row['author_id']) if row['author_id'] is not None else None,
                  timestamp=row['timestamp'] or 0, channel_id=channel, message_id=message,
                  reply_to_id=str(row['reply_to_id']) if row['reply_to_id'] else None,
                  location=channels[channel], locator=f"https://discord.com/channels/{row['guild_id']}/{channel}/{message}")


class Exporter:
    def __init__(self, state, configs, session: aiohttp.ClientSession):
        self.state, self.configs, self.session = state, configs, session
        self.lock = asyncio.Lock()
        self.initialized = False
        self.last_scan = 0
        self.policy = None
        self.storage_id = None

    async def request(self, endpoint: str, body: dict):
        key = os.environ.get('WENDY_MEMORY_SERVICE_TOKEN', '')
        if len(key) < 32:
            raise ValueError('Set a memory service token of at least 32 characters')
        url = os.getenv('WENDY_MEMORY_URL', 'http://127.0.0.1:8950').rstrip('/')
        async with self.session.post(url + endpoint, json=body, headers={'Authorization': 'Bearer ' + key},
                                     timeout=aiohttp.ClientTimeout(total=75)) as response:
            response.raise_for_status()
            return await response.json()

    async def send(self, sources=(), deleted=(), **extras):
        return await self.request('/v1/ingest', {'sources': [s.model_dump() for s in sources],
                                                'deleted': list(deleted), **extras})

    async def sync(self, force_files=False):
        async with self.lock:
            status = await self.request('/v1/status', {})
            if self.storage_id != status['storage_id']:
                self.initialized = False
                self.storage_id = status['storage_id']
            cfg = settings()
            channels = conversations(self.state, self.configs(), cfg)
            policy = digest([cfg, channels])
            if not self.initialized or policy != self.policy:
                # High-water before keyset scan + replay handles backfilled low IDs,
                # edits/deletes during scanning, and process death at any point.
                cursor = self.state.memory_version()
                epoch, after = str(uuid.uuid4()), 0
                while rows := self.state.memory_page(after):
                    sources = [s for r in rows if (s := message_source(r, channels))]
                    await self.send(sources, epoch=epoch)
                    after = rows[-1]['message_id']
                await self.send(sweep={'epoch': epoch, 'kinds': ['chat']})
                self.state.memory_set('cursor', cursor)
                self.initialized, self.policy = True, policy
                force_files = True
            cursor = self.state.memory_get('cursor', 0)
            # A finite checkpoint keeps a busy channel from starving the request.
            target = self.state.memory_version()
            while cursor < target:
                rows = [r for r in self.state.memory_changes(cursor) if r['seq'] <= target]
                if not rows:
                    break
                sources, deleted = [], []
                for row in rows:
                    source = message_source(row, channels) if row['message_id'] else None
                    if source:
                        sources.append(source)
                    else:
                        deleted.append('discord:' + str(row['changed_id']))
                await self.send(sources, deleted)
                cursor = rows[-1]['seq']
                self.state.memory_set('cursor', cursor)  # Only after durable remote acknowledgment.
            if force_files or time.monotonic() - self.last_scan >= cfg.get('file_scan_seconds', 60):
                await self.scan_files(channels, cfg)
                self.last_scan = time.monotonic()
            await self.send(coverage={'outbox_seq': cursor, 'synced_at': time.time(),
                                      'history': self.state.memory_get('history', {})})
            self.state.memory_acknowledge(cursor)

    async def scan_files(self, channels, cfg):
        roots = [(journal_dir(folder), 'journal', 'channel:' + channel, folder)
                 for channel, folder in channels.items()]
        if cfg.get('include_profiles', False):
            roots.append((WENDY_BASE / 'claude_fragments' / 'people', 'profile', 'people', 'people profiles'))
        epoch = str(uuid.uuid4())
        for root, kind, domain, location in roots:
            for path in sorted(root.rglob('*.md')):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    continue
                try:
                    if path.stat().st_size > 500000:
                        raise ValueError('file size limit')
                    text = path.read_text(encoding='utf-8')
                    relative = path.relative_to(WENDY_BASE).as_posix()
                    source = Source(id='file:' + relative, domain=domain, kind=kind, text=text,
                                    speaker='Wendy (written note)', timestamp=int(path.stat().st_mtime),
                                    speaker_role='note',
                                    location=location, locator=relative)
                    await self.send([source], epoch=epoch)
                except (OSError, UnicodeError, ValueError) as exc:
                    _LOG.warning('Memory file excluded (%s): %s', type(exc).__name__, path.name)
        await self.send(sweep={'epoch': epoch, 'kinds': ['journal', 'profile']})

    async def run(self):
        while True:
            try:
                await self.sync()
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                _LOG.warning('Memory export delayed; will retry')
            await asyncio.sleep(settings().get('sync_seconds', 15))
