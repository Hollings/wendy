"""Wendy's compact natural-language historical research interface."""
from __future__ import annotations

import json
import os
from typing import Literal

import aiohttp
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from memory_protocol import render

mcp = FastMCP('wendy-memory')
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


async def call(endpoint: str, body: dict) -> dict:
    port = int(os.getenv('WENDY_PROXY_PORT', '8945'))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as session:
        async with session.post(f'http://127.0.0.1:{port}/api/memory/{endpoint}', json=body,
                                headers={'Authorization': 'Bearer ' + os.environ.get('WENDY_API_TOKEN', '')}) as response:
            result = await response.json()
            if response.status != 200:
                raise ValueError(result.get('error', 'Memory research unavailable'))
            return result


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def research_memory(question: str, context: str = '', depth: Literal['standard', 'deep'] = 'standard',
                          followup_to: str | None = None) -> str:
    """Research historical chats and journals; get a concise answer with original evidence.

    Ask a precise natural-language question with names/timeframes when known. Context
    may explain what you already know. Use deep for difficult chronology or conflicting
    evidence. A followup_to research ID continues a prior investigation without loading
    its search trace. This never reads the live inbox or marks messages as read.
    """
    return render(await call('research', {'question': question, 'context': context,
                                          'depth': depth, 'followup_to': followup_to}))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def open_memory_evidence(research_id: str, refs: list[str], cursor: int = 0) -> str:
    """Expand selected references (e.g. ['S1']) from previous research. Cursor pages long evidence."""
    return json.dumps(await call('evidence', {'research_id': research_id, 'refs': refs, 'cursor': cursor}), ensure_ascii=False)


if __name__ == '__main__':
    mcp.run()
