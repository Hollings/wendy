"""Source authority, transactional search index, and durable Hindsight work queue."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from memory_protocol import Scope, Source, digest, visible


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, domain TEXT NOT NULL, kind TEXT NOT NULL,
                body TEXT NOT NULL, revision TEXT NOT NULL, epoch TEXT, group_key TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sources_group ON sources(group_key);
            CREATE INDEX IF NOT EXISTS sources_domain ON sources(domain,kind);
            CREATE INDEX IF NOT EXISTS sources_message ON sources(domain,CAST(json_extract(body,'$.message_id') AS INTEGER));
            CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(id UNINDEXED, text);
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, domain TEXT NOT NULL, group_key TEXT NOT NULL,
                body TEXT NOT NULL, members TEXT NOT NULL, revision TEXT NOT NULL,
                synced TEXT, deleted INTEGER NOT NULL DEFAULT 0,
                operation TEXT, pending TEXT, retry_at REAL NOT NULL DEFAULT 0, error TEXT
            );
            CREATE INDEX IF NOT EXISTS documents_group ON documents(group_key);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('revision', '0');
            CREATE TABLE IF NOT EXISTS research (
                id TEXT PRIMARY KEY, origin TEXT NOT NULL, policy TEXT NOT NULL,
                created REAL NOT NULL, body TEXT NOT NULL
            );
        ''')
        if not self.get_meta('storage_id'):
            with self.db:
                self.set_meta('storage_id', str(uuid.uuid4()))

    def get_meta(self, key: str, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    @staticmethod
    def group(source: Source) -> str:
        # Stable ten-minute windows; bounded parts below. Thread IDs are domains.
        return f'{source.domain}:chat:{source.timestamp // 600}' if source.kind == 'chat' else source.id

    def ingest(self, sources: list[Source], deleted: list[str], epoch: str | None = None):
        affected = set()
        with self.db:
            for source in sources:
                old = self.db.execute('SELECT revision,group_key FROM sources WHERE id=?', (source.id,)).fetchone()
                if old and old['revision'] == source.revision:
                    if epoch:
                        self.db.execute('UPDATE sources SET epoch=? WHERE id=?', (epoch, source.id))
                    continue
                group = self.group(source)
                affected.add(group)
                if old:
                    affected.add(old['group_key'])
                self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?)',
                                (source.id, source.domain, source.kind, source.model_dump_json(), source.revision, epoch, group))
                # FTS5's unindexed ID requires a full scan; new sources have
                # no previous entry to remove.
                if old:
                    self.db.execute('DELETE FROM source_fts WHERE id=?', (source.id,))
                self.db.execute('INSERT INTO source_fts VALUES (?,?)',
                                (source.id, f'{source.speaker} {source.location}\n{source.text}'))
            for source_id in deleted:
                row = self.db.execute('SELECT group_key FROM sources WHERE id=?', (source_id,)).fetchone()
                if row:
                    affected.add(row['group_key'])
                    self.db.execute('DELETE FROM sources WHERE id=?', (source_id,))
                    self.db.execute('DELETE FROM source_fts WHERE id=?', (source_id,))
            for group in affected:
                self._rebuild_group(group)
            if affected:
                self.set_meta('revision', self.get_meta('revision', 0) + 1)
                # Even a previously cited correction may invalidate the interpretation.
                self.db.execute('DELETE FROM research')

    def sweep(self, epoch: str, kinds: list[str]):
        rows = self.db.execute('SELECT id,epoch,kind FROM sources').fetchall()
        self.ingest([], [r['id'] for r in rows if r['kind'] in kinds and r['epoch'] != epoch])

    def _rebuild_group(self, group: str):
        records = [Source.model_validate_json(r[0]) for r in self.db.execute(
            'SELECT body FROM sources WHERE group_key=?', (group,))]
        records.sort(key=lambda s: (s.timestamp, int(s.message_id or 0), s.id))
        parts: list[tuple[str, dict]] = []
        text, members = '', {}
        for source in records:
            header = (f'[{source.id}] {source.speaker} ({source.speaker_role}; '
                      f'{datetime.fromtimestamp(source.timestamp, UTC).isoformat()}) [{source.kind}]\n')
            # Large files/messages can span documents; evidence always resolves to original.
            for start in range(0, max(1, len(source.text)), 10000):
                block = header + source.text[start:start + 10000] + '\n'
                if text and len(text) + len(block) > 12000:
                    parts.append((text, members))
                    text, members = '', {}
                text += block
                members[source.id] = source.revision
        if text:
            parts.append((text, members))
        old = {r['id']: r for r in self.db.execute('SELECT * FROM documents WHERE group_key=?', (group,))}
        for part, (text, members) in enumerate(parts):
            doc_id = digest([group, part])
            revision = digest([text, members])
            previous = old.pop(doc_id, None)
            if previous and previous['revision'] == revision and not previous['deleted']:
                continue
            self.db.execute('''INSERT INTO documents (id,domain,group_key,body,members,revision)
                VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,
                members=excluded.members,revision=excluded.revision,deleted=0,retry_at=0,error=NULL''',
                (doc_id, records[0].domain, group, text, json.dumps(members), revision))
        for doc_id in old:
            self.db.execute("UPDATE documents SET deleted=1,revision='deleted',body='',members='{}',retry_at=0 WHERE id=?", (doc_id,))

    def read(self, source_id: str, scope: Scope) -> Source | None:
        row = self.db.execute('SELECT body FROM sources WHERE id=?', (source_id,)).fetchone()
        source = Source.model_validate_json(row[0]) if row else None
        return source if source and visible(source, scope) else None

    def search(self, query: str, scope: Scope, limit=8, kind=None, after=None, before=None) -> list[Source]:
        terms = re.findall(r'\w+', query, re.UNICODE)[:24]
        if not terms:
            return []
        match = ' OR '.join('"' + word.replace('"', '""') + '"' for word in terms)
        # Filter authority before ranking/limit so hidden hits cannot crowd out visible ones.
        results = []
        for row in self.db.execute('''SELECT s.body FROM source_fts f JOIN sources s ON s.id=f.id
            WHERE source_fts MATCH ? ORDER BY rank''', (match,)):
            source = Source.model_validate_json(row[0])
            if visible(source, scope) and (not kind or source.kind == kind) and (
                after is None or source.timestamp >= after
            ) and (before is None or source.timestamp <= before):
                results.append(source)
                if len(results) >= limit:
                    break
        return results

    def neighbors(self, source: Source, scope: Scope, count=2) -> list[Source]:
        if source.kind != 'chat':
            return []
        result = []
        for operator, order in (('<', 'DESC'), ('>', 'ASC')):
            rows = self.db.execute(f'''SELECT body FROM sources WHERE domain=? AND kind='chat'
                AND CAST(json_extract(body,'$.message_id') AS INTEGER) {operator} ?
                AND CAST(json_extract(body,'$.message_id') AS INTEGER)<=?
                ORDER BY CAST(json_extract(body,'$.message_id') AS INTEGER) {order} LIMIT ?''',
                (source.domain, int(source.message_id), int(scope.cutoffs.get(source.channel_id, '0')), count))
            result.extend(Source.model_validate_json(r[0]) for r in rows)
        if source.reply_to_id:
            reply = self.read('discord:' + source.reply_to_id, scope)
            if reply and all(s.id != reply.id for s in result):
                result.append(reply)
        return sorted(result, key=lambda s: int(s.message_id))

    def document_sources(self, doc_id: str, scope: Scope, fact_revision: str | None = None) -> list[Source]:
        row = self.db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
        if not row or row['deleted'] or row['synced'] != row['revision'] or row['operation']:
            return []
        if fact_revision is not None and row['revision'] != fact_revision:
            return []
        sources = []
        for source_id, revision in json.loads(row['members']).items():
            source = self.read(source_id, scope)
            if not source or source.revision != revision:
                return []  # One hidden/stale member suppresses the entire episode.
            sources.append(source)
        return sources

    def coverage(self, scope: Scope) -> dict:
        counts = {}
        for domain in scope.domains:
            cutoff = int(scope.cutoffs.get(domain.removeprefix('channel:'), '0'))
            for row in self.db.execute('''SELECT kind,COUNT(*) AS n FROM sources WHERE domain=?
                AND (kind!='chat' OR CAST(json_extract(body,'$.message_id') AS INTEGER)<=?) GROUP BY kind''', (domain, cutoff)):
                counts[row['kind']] = counts.get(row['kind'], 0) + row['n']
        pending = sum(1 for r in self.db.execute('SELECT domain,revision,synced,operation,error FROM documents')
                      if r['domain'] in scope.domains and (r['revision'] != r['synced'] or r['operation']))
        return {'source_revision': self.get_meta('revision', 0), 'sources': counts,
                'hindsight_state': 'pending' if pending else 'current', 'pending_documents': pending,
                'sync': self.get_meta('sync', {}), 'history': {k: v for k, v in self.get_meta('history', {}).items()
                                                            if 'channel:' + k in scope.domains},
                'limitations': ['Only cached/backfilled text and current journal/profile files are indexed.',
                                'Attachment contents and Claude session transcripts are not indexed.']}

    def save_research(self, result: dict, scope: Scope):
        with self.db:
            self.db.execute('DELETE FROM research WHERE created<?', (time.time() - 7 * 86400,))
            self.db.execute('INSERT OR REPLACE INTO research VALUES (?,?,?,?,?)',
                            (result['research_id'], scope.origin, scope.policy, time.time(), json.dumps(result)))

    def research(self, research_id: str, scope: Scope) -> dict | None:
        row = self.db.execute('SELECT * FROM research WHERE id=? AND origin=? AND policy=? AND created>?',
                              (research_id, scope.origin, scope.policy, time.time() - 7 * 86400)).fetchone()
        if not row:
            return None
        result = json.loads(row['body'])
        for citation in result['sources']:
            source = self.read(citation['source_id'], scope)
            if not source or source.revision != citation['revision']:
                return None
        return result
