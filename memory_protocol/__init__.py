"""Versioned wire contracts shared by the bot and memory service."""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(WireModel):
    seconds: int = Field(ge=5, le=60)
    calls: int = Field(ge=1, le=16)
    source_bytes: int = Field(ge=1000, le=96000)
    answer_chars: int = Field(ge=100, le=4800)
    citations: int = Field(ge=1, le=6)


@lru_cache(maxsize=2)
def budget(depth: str) -> Budget:
    default = Path(__file__).resolve().parents[1] / 'config' / 'memory_limits.json'
    limits = json.loads(Path(os.getenv('WENDY_MEMORY_LIMITS', str(default))).read_text(encoding='utf-8'))
    return Budget.model_validate(limits[depth])


class ResearchRequest(WireModel):
    question: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=4000)
    depth: Literal["standard", "deep"] = "standard"
    followup_to: str | None = Field(default=None, max_length=64)


class EvidenceRequest(WireModel):
    research_id: str = Field(max_length=64)
    refs: list[str] = Field(min_length=1, max_length=6)
    cursor: int = Field(default=0, ge=0, le=1_000_000)


class Scope(WireModel):
    origin: str
    domains: list[str] = Field(min_length=1, max_length=500)
    # Missing channel cutoff denies all chat from that channel. IDs stay strings.
    cutoffs: dict[str, str]
    policy: str


class Source(WireModel):
    id: str = Field(max_length=256)
    domain: str = Field(max_length=128)
    kind: Literal["chat", "journal", "profile"]
    text: str = Field(max_length=500_000)
    speaker: str = Field(max_length=256)
    speaker_role: Literal['human', 'bot', 'webhook', 'note'] = 'human'
    timestamp: int
    location: str = Field(max_length=512)
    locator: str = Field(max_length=1024)
    channel_id: str | None = None
    message_id: str | None = None
    author_id: str | None = None
    reply_to_id: str | None = None

    @property
    def revision(self) -> str:
        return digest(self.model_dump())


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def read_limited(stream, limit: int) -> bytes:
    """Read to EOF across network chunks, enforcing a hard byte limit."""
    chunks, size = [], 0
    while chunk := await stream.read(min(65536, limit + 1 - size)):
        size += len(chunk)
        if size > limit:
            raise ValueError('Response exceeded byte limit')
        chunks.append(chunk)
    return b''.join(chunks)


def visible(source: Source, scope: Scope) -> bool:
    if source.domain not in scope.domains:
        return False
    if source.kind == "chat":
        return int(source.message_id or 0) <= int(scope.cutoffs.get(source.channel_id or "", "0"))
    return True


def render(result: dict) -> str:
    """Exactly one compact representation enters Wendy's context."""
    lines = [f"Memory research {result['research_id']} ({result['status']})", result['answer']]
    for source in result.get('sources', []):
        lines.append(f"[{source['ref']}] {source['speaker']} · {source['date']} · {source['location']} "
                     f"{source['locator']}\n{source['excerpt']}")
    if result.get('limitations'):
        lines.append('Limitations: ' + '; '.join(result['limitations']))
    coverage = result.get('coverage', {})
    lines.append(f"Index revision: {coverage.get('source_revision', '?')}; "
                 f"Hindsight: {coverage.get('hindsight_state', 'unknown')}")
    return '\n\n'.join(lines)
