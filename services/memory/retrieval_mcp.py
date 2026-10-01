"""The researcher's entire tool surface. Token is scoped to one short-lived run."""
from __future__ import annotations

import json
import os

import aiohttp
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('memory-retrieval')


async def call(name, arguments):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=22)) as session:
        async with session.post(os.environ['MEMORY_GATEWAY_URL'],
                                headers={'Authorization': 'Bearer ' + os.environ['MEMORY_RETRIEVAL_TOKEN']},
                                json={'tool': name, 'arguments': arguments}) as response:
            response.raise_for_status()
            return json.dumps(await response.json(), ensure_ascii=False)


@mcp.tool(structured_output=False)
async def recall_memory(query: str) -> str:
    """Search semantic, graph and temporal memory. Results are leads; read originals before citing."""
    return await call('recall_memory', {'query': query})


@mcp.tool(structured_output=False)
async def search_sources(query: str, kind: str | None = None, after: int | None = None, before: int | None = None) -> str:
    """Search original chat, journal and profile text; optional Unix timestamp bounds."""
    return await call('search_sources', {'query': query, 'kind': kind, 'after': after, 'before': before})


@mcp.tool(structured_output=False)
async def read_sources(source_ids: list[str], neighbors: bool = True, offset: int = 0) -> str:
    """Read up to four original sources, nearby messages and reply parents. Offset pages long files."""
    return await call('read_sources', {'source_ids': source_ids, 'neighbors': neighbors, 'offset': offset})


@mcp.tool(structured_output=False)
async def inspect_coverage() -> str:
    """Inspect source freshness, ingestion lag and known gaps in the historical archive."""
    return await call('inspect_coverage', {})


if __name__ == '__main__':
    mcp.run()
