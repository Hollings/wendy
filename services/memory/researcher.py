"""Fresh Claude process, source-checked citations, and bounded answers."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiohttp
from pydantic import Field, ValidationError

from memory_protocol import ResearchRequest, Scope, WireModel, read_limited

from .gateway import Gateway
from .index import Index

_LOG = logging.getLogger(__name__)


def safe_failure_code(exc: Exception) -> str:
    if isinstance(exc, ResearchFailure):
        return str(exc)
    if isinstance(exc, ValidationError):
        return 'invalid_answer_schema'
    if isinstance(exc, TimeoutError):
        return 'research_timeout'
    if isinstance(exc, ValueError):
        return {
            'Answer budget exceeded': 'answer_budget_exceeded',
            'Invalid citation references': 'invalid_citation_references',
            'Answer has no original evidence': 'missing_original_citations',
            'Source changed or was not read': 'source_changed_or_unread',
            'Excerpt does not match retrieved original': 'excerpt_mismatch',
        }.get(str(exc), 'invalid_research_response')
    return type(exc).__name__


class Citation(WireModel):
    ref: str = Field(pattern=r'^S[1-6]$')
    source_id: str
    revision: str
    excerpt: str = Field(min_length=1, max_length=400)


class Answer(WireModel):
    status: Literal['answered', 'partial', 'no_evidence']
    answer: str = Field(max_length=4800)
    citations: list[Citation] = Field(max_length=6)
    limitations: list[str] = Field(max_length=5)


class ResearchFailure(ValueError):
    """Safe diagnostic code, never a provider response or retrieved source text."""


def failure_code(stdout: bytes, stderr: bytes, exit_code: int) -> str:
    text = (stdout + stderr).decode(errors='replace').lower()
    for needle, code in [('oauth access token has been revoked', 'oauth_revoked'),
                         ('git-bash', 'git_bash_missing'), ('git bash', 'git_bash_missing'),
                         ('unknown option', 'unsupported_cli_flag'), ('not logged in', 'authentication_required'),
                         ('invalid api key', 'invalid_credentials'), ('oauth token has expired', 'oauth_expired'),
                         ('credit balance', 'provider_balance'), ('model', 'model_or_cli_error')]:
        if needle in text:
            return code
    return f'cli_exit_{exit_code}'


def command(cli: str, config: Path, prompt: str, model: str, turns: int) -> list[str]:
    return [cli, '-p', '--restricted', '--setting-sources', '', '--disable-slash-commands',
            '--settings', '{"disableAllHooks":true,"autoMemoryEnabled":false}',
            '--tools', '', '--strict-mcp-config', '--mcp-config', str(config),
            '--permission-mode', 'dontAsk', '--allowedTools', 'mcp__retrieval__*',
            '--no-session-persistence', '--model', model, '--max-turns', str(turns),
            '--output-format', 'json', '--json-schema', json.dumps(Answer.model_json_schema()),
            '--system-prompt', prompt]


async def terminate(proc):
    if proc.returncode is not None:
        return
    if os.name == 'posix':
        os.killpg(proc.pid, signal.SIGKILL)
    else:
        killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(proc.pid), '/T', '/F',
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    await proc.wait()


async def run_claude(request: ResearchRequest, gateway: Gateway, token: str, url: str,
                     previous: dict | None = None) -> Answer:
    root = Path(__file__).resolve().parents[2]
    system = (root / 'config' / 'memory_researcher.txt').read_text(encoding='utf-8')
    cli = os.getenv('WENDY_MEMORY_CLAUDE_PATH') or shutil.which('claude')
    if not cli:
        raise OSError('Research CLI unavailable')
    # Neither the controller's session capability nor Hindsight/provider/service keys
    # are inherited. Restricted settings and a clean HOME/cwd prevent customization
    # discovery. --bare cannot be used: it explicitly disables subscription OAuth.
    allowed_env = ('PATH', 'SystemRoot', 'COMSPEC', 'TEMP', 'TMP', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                   'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_GIT_BASH_PATH')
    env = {k: os.environ[k] for k in allowed_env if k in os.environ}
    with tempfile.TemporaryDirectory(prefix='wendy-memory-') as directory:
        home = Path(directory)
        env.update(HOME=directory, USERPROFILE=directory, CLAUDE_CONFIG_DIR=str(home / '.claude'),
                   PYTHONPATH=str(root), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        config = home / 'mcp.json'
        config.write_text(json.dumps({'mcpServers': {'retrieval': {
            'command': sys.executable, 'args': ['-m', 'services.memory.retrieval_mcp'],
            'env': {'MEMORY_GATEWAY_URL': url, 'MEMORY_RETRIEVAL_TOKEN': token, 'PYTHONPATH': str(root)},
        }}}), encoding='utf-8')
        config.chmod(0o600)
        argv = command(cli, config, system, os.getenv('WENDY_MEMORY_MODEL', 'sonnet'), gateway.max_calls + 2)
        proc = await asyncio.create_subprocess_exec(*argv, cwd=home, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=os.name == 'posix', limit=2_000_000)
        payload = {'question': request.question, 'context': request.context,
                   'answer_max_chars': gateway.budget.answer_chars,
                   'max_citations': gateway.budget.citations,
                   'previous_answer': previous}
        try:
            proc.stdin.write(json.dumps(payload).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            stdout, stderr = await asyncio.gather(read_limited(proc.stdout, 2_000_000), read_limited(proc.stderr, 64000))
            await proc.wait()
            if proc.returncode:
                raise ResearchFailure(failure_code(stdout, stderr, proc.returncode))
            result = json.loads(stdout)
            if result.get('is_error'):
                raise ResearchFailure(failure_code(stdout, stderr, proc.returncode))
            gateway.usage = {'total_cost_usd': result.get('total_cost_usd'),
                             'input_tokens': result.get('usage', {}).get('input_tokens'),
                             'output_tokens': result.get('usage', {}).get('output_tokens')}
            return Answer.model_validate(result['structured_output'])
        finally:
            await terminate(proc)


def validate(answer: Answer, gateway: Gateway, request: ResearchRequest) -> tuple[list[dict], str]:
    if len(answer.citations) > gateway.budget.citations or len(answer.answer) > gateway.budget.answer_chars:
        raise ValueError('Answer budget exceeded')
    refs = [c.ref for c in answer.citations]
    if len(set(refs)) != len(refs) or set(re.findall(r'\[(S\d+)\]', answer.answer)) != set(refs):
        raise ValueError('Invalid citation references')
    if answer.status in ('answered', 'partial') and not refs:
        raise ValueError('Answer has no original evidence')
    sources = []
    for c in answer.citations:
        source = gateway.index.read(c.source_id, gateway.scope)
        receipt = gateway.seen.get(c.source_id)
        if not source or source.revision != c.revision or not receipt or receipt[0] != c.revision:
            raise ValueError('Source changed or was not read')
        if c.excerpt not in source.text or not any(c.excerpt in text for text in receipt[1]):
            raise ValueError('Excerpt does not match retrieved original')
        sources.append({'ref': c.ref, 'source_id': c.source_id, 'revision': source.revision,
                        'kind': source.kind, 'speaker': source.speaker,
                        'date': datetime.fromtimestamp(source.timestamp, UTC).isoformat(),
                        'location': source.location, 'locator': source.locator, 'excerpt': c.excerpt})
    return sources, answer.answer


class Researcher:
    def __init__(self, index: Index, backend, gateway_url: str, runner=None):
        if runner is None:
            provider = os.getenv('WENDY_MEMORY_RESEARCHER', 'gemini')
            if provider == 'gemini':
                from .gemini_researcher import run_gemini
                runner = run_gemini
            elif provider == 'deepseek':
                from .deepseek_researcher import run_deepseek
                runner = run_deepseek
            elif provider == 'claude':
                runner = run_claude
            else:
                raise ValueError('WENDY_MEMORY_RESEARCHER must be claude, gemini, or deepseek')
        self.index, self.backend, self.gateway_url, self.runner = index, backend, gateway_url, runner
        self.capabilities: dict[str, Gateway] = {}
        self.active = 0
        self.max_active = int(os.getenv('WENDY_MEMORY_CONCURRENCY', '2'))

    async def research(self, request: ResearchRequest, scope: Scope, mode='combined') -> dict:
        research_id, token = str(uuid.uuid4()), uuid.uuid4().hex + uuid.uuid4().hex
        gateway = Gateway(self.index, self.backend, scope, request.depth, mode)
        coverage = self.index.coverage(scope)
        result = {'version': 1, 'research_id': research_id, 'status': 'unavailable',
                  'answer': 'Memory research is unavailable; try again later.', 'sources': [],
                  'limitations': [], 'coverage': coverage}
        if self.active >= self.max_active:
            result['limitations'] = ['The research service is busy.']
            return result
        previous = None
        if request.followup_to:
            previous = self.index.research(request.followup_to, scope)
            if not previous:
                result['limitations'] = ['The prior research expired or its evidence changed; ask a fresh question.']
                return result
        self.active += 1
        self.capabilities[token] = gateway
        started = time.monotonic()
        try:
            async with asyncio.timeout(gateway.budget.seconds):
                answer = await self.runner(request, gateway, token, self.gateway_url, previous)
                sources, text = validate(answer, gateway, request)
            result.update(status=answer.status, answer=text, sources=sources,
                          limitations=[s[:300] for s in answer.limitations])
        except (TimeoutError, OSError, ValueError, KeyError, aiohttp.ClientError) as exc:
            # An unfinished run never returns an unchecked model draft or raw trace.
            result['limitations'] = ['Research did not complete with a valid, current evidence-backed answer.']
            result['failure_code'] = safe_failure_code(exc)
            _LOG.warning('Memory research failed: code=%s depth=%s calls=%d',
                         result['failure_code'], request.depth, gateway.calls)
        finally:
            self.capabilities.pop(token, None)
            self.active -= 1
        result['coverage'] = self.index.coverage(scope)
        if gateway.backend_unavailable:
            result['coverage']['hindsight_state'] = 'unavailable'
            result['limitations'].append('Hindsight was unavailable; original-source search was used.')
        result['metrics'] = {'seconds': round(time.monotonic() - started, 2), 'calls': gateway.calls,
                             'tool_events': gateway.events, 'usage': gateway.usage, 'mode': mode}
        self.index.save_research(result, scope)
        return result
