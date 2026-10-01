"""Resumable Discord history import, separate from delivery and generation."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import discord

from .memory_export import conversations, settings


async def import_channel(channel, state, bot, audit=False):
    key = 'backfill:' + str(channel.id)
    audit_key = 'audit:' + str(channel.id)
    snapshot = state.memory_get(audit_key) if audit else None
    if audit and not snapshot:
        snapshot = {'run_id': uuid.uuid4().hex, 'cursor': 0,
                    'cutoff': discord.utils.time_snowflake(datetime.now(UTC), high=True)}
        state.memory_set(audit_key, snapshot)
    cursor = snapshot['cursor'] if audit else state.memory_get(key, 0)
    history = state.memory_get('history', {})
    status = history.setdefault(str(channel.id), {})
    status.update(state='running', started_at=time.time())
    state.memory_set('history', history)
    try:
        while True:
            page = [m async for m in channel.history(limit=100, oldest_first=True,
                    after=discord.Object(id=cursor) if cursor else None,
                    before=discord.Object(id=snapshot['cutoff'] + 1) if audit else None)]
            if not page:
                break
            for message in page:
                # Use the existing canonical renderer (mentions, forwards, attachments).
                bot._cache_message(message, refresh=True)
                cursor = max(cursor, message.id)
            if audit:
                state.memory_audit_page(snapshot['run_id'], [m.id for m in page])
                snapshot['cursor'] = cursor
                state.memory_set(audit_key, snapshot)
            else:
                state.memory_set(key, cursor)
            await asyncio.sleep(0.25)  # discord.py also honors API rate-limit responses.
        if audit:
            state.memory_audit_complete(snapshot['run_id'], channel.id, snapshot['cutoff'], cursor)
        status.update(state='complete_to_checkpoint', last_id=str(cursor), checked_at=time.time(),
                      audited=audit, limitation='Coverage ends at this checkpoint; inaccessible or undiscovered threads are excluded.')
        if not audit:
            status['limitation'] = 'Previously imported edits/deletions while offline need a fresh audit.'
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        status.update(state='incomplete', reason=type(exc).__name__, last_id=str(cursor))
    finally:
        history = state.memory_get('history', {})
        history[str(channel.id)] = status
        state.memory_set('history', history)


async def run(bot, state):
    await bot.wait_until_ready()
    cfg = settings()
    if not cfg.get('backfill', False):
        return
    # configured channels, their active threads and accessible archived threads
    channels = conversations(state, bot.channel_configs, cfg)
    visited = set()

    async def one(channel):
        if channel.id in visited:
            return
        visited.add(channel.id)
        if isinstance(channel, discord.Thread) and not state.get_thread_folder(channel.id):
            parent = channels.get(str(channel.parent_id))
            if not parent:
                return
            state.register_thread(channel.id, channel.parent_id, f'{parent}_thread_{channel.id}', channel.name)
        if hasattr(channel, 'history'):
            await import_channel(channel, state, bot, audit=cfg.get('backfill_audit', False))

    for channel_id in list(channels):
        try:
            channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
            await one(channel)
            for thread in getattr(channel, 'threads', []):
                await one(thread)
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                modes = [{}, {'private': True, 'joined': True}] if isinstance(channel, discord.TextChannel) else [{}]
                for mode in modes:
                    try:
                        async for thread in channel.archived_threads(limit=None, **mode):
                            await one(thread)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        history = state.memory_get('history', {})
                        history.setdefault(channel_id, {})['thread_discovery_gap'] = type(exc).__name__
                        state.memory_set('history', history)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            history = state.memory_get('history', {})
            history[channel_id] = {'state': 'incomplete', 'reason': type(exc).__name__}
            state.memory_set('history', history)
