"""BD-backed task controller. Shared workspaces; durable, explicit recovery."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

from .beads import BeadsClient, BeadsError
from .config import MODEL_MAP, parse_channel_configs, resolve_model
from .paths import WENDY_BASE, beads_dir, channel_dir
from .task_store import ACTIVE_PHASES, TaskStore, quota_config
from .worker_runtime import (
    AGENT_PROMPT_TEMPLATE as AGENT_PROMPT_TEMPLATE,
)
from .worker_runtime import (
    WorkerRuntime,
    extract_result_summary,
    process_identity,
    stop_orphan,
)

_LOG = logging.getLogger(__name__)
CONCURRENCY = max(1, int(os.getenv('ORCHESTRATOR_CONCURRENCY', '3')))
POLL_INTERVAL = max(1, int(os.getenv('ORCHESTRATOR_POLL_INTERVAL', '30')))
AGENT_TIMEOUT = max(1, int(os.getenv('ORCHESTRATOR_AGENT_TIMEOUT', '14400')))
CLOSED_TASK_GRACE_PERIOD = max(1, int(os.getenv('ORCHESTRATOR_CLOSED_GRACE_PERIOD', '30')))
RUNNER_ASSIGNEE = 'task-runner'


def tasks_to_reopen(in_progress: list[dict], running_task_ids: set[str]) -> list[str]:
    """Legacy inspection helper; the controller now holds orphaned work for review."""
    return [t['id'] for t in in_progress if t.get('id') and t['id'] not in running_task_ids
            and (t.get('assignee') or '') in ('', RUNNER_ASSIGNEE)]


def select_task_model(value: str | None) -> str:
    value = (value or os.getenv('WENDY_TASK_DEFAULT_MODEL', 'opus')).strip().lower()
    if value not in MODEL_MAP and not any(value.startswith(f'claude-{family}-') for family in MODEL_MAP):
        raise ValueError('Unknown task model. Use wtask models for aliases or a full Claude model ID in a listed family.')
    return resolve_model(value, allow_env_override=False)


class TaskRunner:
    def __init__(self, *, store=None, beads=None, runtime=None):
        self.store = store or TaskStore()
        self.beads = beads or BeadsClient()
        self.runtime = runtime or WorkerRuntime()
        self.agents = {}
        self.channels = {}
        self.owner = str(uuid.uuid4())
        self.wake = asyncio.Event()
        self._command_lock = asyncio.Lock()
        self._offset = 0
        self.available = False

    async def run(self):
        quota_config()  # Invalid policy fails closed before worker launch.
        self.channels = {cfg.get('_folder') or cfg['name']: cid for cid, cfg in parse_channel_configs().items()
                         if cfg.get('beads_enabled')}
        if not self.channels:
            return
        while not self.store.acquire_runner(self.owner, time.time()):
            await asyncio.sleep(10)
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            from .cli import setup_channel_folder, setup_wendy_scripts
            setup_wendy_scripts()
            for queue in self.channels:
                setup_channel_folder(queue, beads_enabled=True)
                if not (beads_dir(queue) / 'config.yaml').exists():
                    await self.beads.run(queue, 'init', '--skip-agents', '--skip-hooks')
            await self._recover()
            self.available = True
            next_scan = 0.0
            while True:
                requested = self.wake.is_set()
                self.wake.clear()
                if heartbeat.done():
                    await heartbeat
                try:
                    due = time.monotonic() >= next_scan
                    before = len(self.agents)
                    await self._check_agents(poll_bd=due)
                    if due or requested or len(self.agents) < before:
                        await self._sync_bd()
                        await self._schedule()
                        await self._snapshot()
                        next_scan = time.monotonic() + POLL_INTERVAL
                except Exception:
                    _LOG.exception('Task controller tick failed; durable state retained')
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            self.available = False
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            for key, agent in list(self.agents.items()):
                await self.runtime.cleanup(agent, stop=True)
                task = self.store.get(key)
                if task['phase'] in ACTIVE_PHASES:
                    attempt = self.store.attempt(task['attempt_id'])
                    if attempt['report'] and task['phase'] != 'stopping':
                        self._finish_report(task, attempt, agent.process.returncode, interrupted=True)
                    else:
                        outcome = 'cancelled' if task['phase'] == 'stopping' else 'interrupted'
                        self.store.finish(key, outcome, 'Controller stopped. Files, session and checkpoints preserved; use wtask resume.')
            self.agents.clear()
            self.store.release_runner(self.owner)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(15)
            if not self.store.acquire_runner(self.owner, time.time()):
                raise RuntimeError('Task runner lease lost; stopping workers')

    async def _recover(self):
        for task in self.store.list():
            if task['phase'] not in ACTIVE_PHASES:
                continue
            attempt = self.store.attempt(task['attempt_id'])
            if attempt:
                await stop_orphan(attempt)
            if attempt and attempt['report'] and task['phase'] != 'stopping':
                self._finish_report(task, attempt, None, interrupted=True)
            else:
                self.store.finish(task['key'], 'interrupted',
                                  'Previous execution interrupted. Inspect files/checkpoints with wtask show, then resume or retry explicitly.')
        for queue in self.channels:
            try:
                existing = await self.beads.json(queue, 'list', '--status', 'in_progress', '--limit', '0')
                for issue in existing:
                    if (issue.get('assignee') or '') not in ('', RUNNER_ASSIGNEE, 'wendy-controller'):
                        continue
                    task = await self._adopt(queue, issue)
                    if task['phase'] == 'queued':
                        self.store.set_phase(task['key'], 'interrupted',
                                             'Legacy in-progress task retained for review. Files remain in the shared workspace; use wtask retry.')
            except (BeadsError, TimeoutError, OSError):
                _LOG.warning('Could not inspect legacy tasks in %s; will not infer deletion', queue, exc_info=True)

    async def _adopt(self, queue, issue):
        try:
            return self.store.find(queue, issue['id'])
        except ValueError:
            pass
        details = await self.beads.show(queue, issue['id'])
        metadata = details.get('metadata') or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        origin = metadata.get('wendy', {}) if isinstance(metadata, dict) else {}
        model = next((label[6:] for label in details.get('labels', []) or [] if label.startswith('model:')), None)
        return self.store.add(bd_id=issue['id'], queue=queue, origin_channel=origin.get('channel_id', self.channels[queue]),
                              source_session=origin.get('session_id'),
                              title=details.get('title', issue['id']), description=details.get('description') or '',
                              context=origin.get('context', 'Imported from raw BD. Origin thread/session is unknown; use the explicit task description.'),
                              workspace=str(channel_dir(queue)), model=select_task_model(origin.get('model') or model),
                              request_id=origin.get('request_id'), prerequisites=origin.get('after', []))

    async def _schedule(self):
        queues = list(self.channels)
        if not queues:
            return
        self._offset = (self._offset + 1) % len(queues)
        for queue in queues[self._offset:] + queues[:self._offset]:
            if len(self.agents) >= CONCURRENCY:
                break
            try:
                ready = await self.beads.ready(queue)
            except (BeadsError, TimeoutError, OSError):
                _LOG.warning('BD queue unavailable: %s', queue, exc_info=True)
                continue
            ready_ids = {issue['id'] for issue in ready}
            for queued in self.store.list(queue):
                if queued['phase'] in ('queued', 'quota_wait') and queued['bd_id'] not in ready_ids:
                    try:
                        details = await self.beads.show(queue, queued['bd_id'])
                        if details.get('status') == 'closed':
                            self.store.stop(queued['key'], 'Queued task closed externally in BD')
                    except (BeadsError, TimeoutError, OSError):
                        pass
            for issue in ready:
                if (issue.get('assignee') or '') not in ('', RUNNER_ASSIGNEE, 'wendy-controller', 'wendy-pending'):
                    continue
                try:
                    task = await self._adopt(queue, issue)
                    if not task['description'].strip() and not self.store.detail(task['key'])['messages']:
                        self.store.set_phase(task['key'], 'needs_input', 'A full task description is required; supply it with wtask tell, then retry.')
                        continue
                    attempt = self.store.reserve(task['key'])
                    if not attempt:
                        continue
                    task = self.store.get(task['key'])
                    try:
                        await self.beads.update(queue, task['bd_id'], 'in_progress')
                        self.store.mark_bd_synced(task['key'], 'in_progress')
                        if self.store.get(task['key'])['phase'] == 'stopping':
                            self.store.finish(task['key'], 'cancelled', 'Stopped before launch; no quota used. Files preserved.')
                            continue
                        self.store.begin_launch(attempt['id'])
                        agent = await self.runtime.launch(task, attempt)
                    except Exception as exc:
                        self.store.launch_failed(task['key'], str(exc), already_charged=bool(attempt['charged']))
                        continue
                    self.agents[task['key']] = agent
                    self.store.launched(task['key'], agent.process.pid, process_identity(agent.process.pid), str(agent.log_path))
                    break
                except (ValueError, BeadsError):
                    _LOG.warning('Cannot schedule %s in %s', issue['id'], queue, exc_info=True)

    def _finish_report(self, task, attempt, exit_code, *, interrupted=False):
        report = json.loads(attempt['report'])
        outcome = report['outcome']
        summary = report['summary']
        if outcome == 'succeeded' and exit_code not in (0, None) and not interrupted:
            outcome = 'failed'
            summary += f' Worker exited with code {exit_code}; inspect its saved report before retrying.'
        if interrupted:
            summary += ' Report was saved before process cleanup; verify the output before publishing.'
        if report.get('artifacts'):
            summary += '\nArtifacts: ' + ', '.join(report['artifacts'])
        self.store.finish(task['key'], outcome, summary)

    async def _check_agents(self, *, poll_bd=True):
        for key, agent in list(self.agents.items()):
            task = self.store.get(key)
            attempt = self.store.attempt(task['attempt_id'])
            if task['phase'] == 'stopping':
                await self.runtime.cleanup(agent, stop=True)
                self.store.finish(key, 'cancelled', task['note'] + ' Files and checkpoints preserved; use wtask resume.')
            elif attempt['report']:
                if agent.report_seen is None:
                    agent.report_seen = time.monotonic()
                if agent.process.returncode is None and time.monotonic() - agent.report_seen < CLOSED_TASK_GRACE_PERIOD:
                    continue
                forced = agent.process.returncode is None
                await self.runtime.cleanup(agent, stop=forced)
                self._finish_report(task, attempt, agent.process.returncode, interrupted=forced)
            elif agent.process.returncode is not None:
                await self.runtime.cleanup(agent)
                summary = extract_result_summary(agent.log_path, 4000)
                self.store.finish(key, 'failed', f'Worker exited ({agent.process.returncode}) without a structured result. '
                                  'Files preserved; inspect wtask show before retrying.\n' + summary)
            elif time.monotonic() - agent.started > AGENT_TIMEOUT:
                await self.runtime.cleanup(agent, stop=True)
                self.store.finish(key, 'timed_out', 'Runtime limit reached. Files, session and checkpoints preserved; use wtask resume.')
            else:
                if not poll_bd:
                    continue
                try:
                    details = await self.beads.show(task['queue'], task['bd_id'])
                    if details.get('status') == 'closed':
                        self.store.stop(key, 'Closed externally in BD')
                        self.wake.set()
                except (BeadsError, TimeoutError, OSError):
                    _LOG.warning('Cannot read BD task %s; worker retained', key)
                continue
            del self.agents[key]

    async def _sync_bd(self):
        for task, desired in self.store.pending_bd_updates():
            try:
                await self.beads.update(task['queue'], task['bd_id'], desired, task['note'])
                self.store.mark_bd_synced(task['key'], desired)
            except (BeadsError, TimeoutError, OSError):
                _LOG.warning('BD status delivery pending for %s', task['key'])

    async def _snapshot(self):
        path = WENDY_BASE / 'shared' / 'beads_snapshot.json'
        try:
            rows = []
            for task in self.store.list():
                rows.append({'id': task['bd_id'], 'title': task['title'], '_channel': task['queue'],
                             'status': 'closed' if task['phase'] == 'succeeded' else
                                       'in_progress' if task['phase'] in ACTIVE_PHASES else 'open',
                             'phase': task['phase'], 'model': task['model'], 'close_reason': task['note']})
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(rows), encoding='utf-8')
            temp.replace(path)
        except OSError:
            _LOG.warning('Could not update task dashboard snapshot', exc_info=True)

    async def command(self, scope: dict, body: dict) -> dict:
        if not self.available:
            raise ValueError('Task controller is starting or unavailable; request was not accepted')
        if body.get('command') == 'start':
            async with self._command_lock:
                result = await self._command(scope, body)
        else:
            result = await self._command(scope, body)
        if body.get('command') in ('start', 'stop', 'resume', 'retry', 'model', 'finish', 'ask'):
            self.wake.set()
        return result

    async def _command(self, scope, body):
        command = body.get('command')
        if scope['role'] == 'worker':
            key = scope['task_key']
            task = self.store.get(key)
            if task['attempt_id'] != scope['attempt_id'] or task['phase'] not in ACTIVE_PHASES:
                raise ValueError('This worker attempt is no longer active; preserve files and exit')
            if command == 'inbox':
                return {'messages': self.store.inbox(key), 'phase': task['phase']}
            if command == 'ack':
                self.store.ack(key, int(body['message_id']))
                return {'acknowledged': int(body['message_id'])}
            if command == 'checkpoint':
                self.store.checkpoint(key, self._text(body, 'text'))
                return {'saved': True}
            if command in ('finish', 'ask'):
                report = body.get('report') if command == 'finish' else {'outcome': 'needs_input', 'summary': self._text(body, 'text')}
                if not isinstance(report, dict):
                    raise ValueError('report must be an object')
                for artifact in report.get('artifacts', []):
                    if not Path(artifact).is_absolute() or not Path(artifact).exists():
                        raise ValueError(f'Artifact must exist at an absolute path: {artifact}')
                self.store.report(key, report)
                return {'saved': True, 'instruction': 'Exit now; your report is durable and Wendy will be notified.'}
            raise ValueError('Workers may only use inbox, ack, checkpoint, ask and finish for their own task')
        queue = scope['queue']
        if queue not in self.channels:
            raise ValueError('Background tasks are not enabled for this channel')
        if command == 'models':
            return self.store.models()
        if command == 'list':
            return {'tasks': self.store.list(queue)}
        if command == 'start':
            title = self._text(body, 'title')
            description = self._text(body, 'description')
            model = select_task_model(body.get('model'))
            priority = int(body.get('priority', 2))
            if not 0 <= priority <= 4:
                raise ValueError('priority must be 0 through 4')
            request_id = str(uuid.UUID(body.get('request_id') or str(uuid.uuid4())))
            previous = next((t for t in self.store.list(queue) if t['request_id'] == request_id), None)
            if previous:
                if previous['title'] != title or previous['description'] != description or previous['model'] != model:
                    raise ValueError('Submission ID already belongs to a different request; use its original parameters or a new ID')
                return {'task': previous, 'accepted': True, 'reused': True}
            matches = await self.beads.json(queue, 'list', '--all', '--label', f'wtask-request:{request_id}', '--limit', '0')
            if matches:
                if not isinstance(matches, list) or len(matches) != 1:
                    raise BeadsError('Ambiguous submission ID; inspect BD before retrying')
                return {'task': await self._adopt(queue, matches[0]), 'accepted': True, 'reused': True}
            workspace = channel_dir(queue).resolve()
            messages = self.store.state.get_recent_messages(scope['channel_id'], limit=20)
            context = json.dumps(messages, ensure_ascii=False, default=str)[-24000:]
            origin = {'schema': 1, 'channel_id': scope['channel_id'], 'session_id': scope.get('session_id'),
                      'context': context, 'model': model, 'request_id': request_id}
            deps = body.get('after') or []
            if not isinstance(deps, list) or any(not isinstance(dep, str) or not dep.strip() for dep in deps):
                raise ValueError('after must be an array of prerequisite task IDs')
            for dep in deps:
                await self.beads.show(queue, dep)
            origin['after'] = deps
            dep_args = ['--deps', ','.join(deps)] if deps else []
            issue = await self.beads.json(queue, 'create', title, '-d', description, '-p', str(priority),
                                          '-l', f'model:{model},wtask-request:{request_id}', '--assignee', 'wendy-pending',
                                          '--metadata', json.dumps({'wendy': origin}), *dep_args)
            if not isinstance(issue, dict) or not issue.get('id'):
                raise BeadsError('BD create returned no task ID; inspect wtask list before resubmitting')
            task = self.store.add(bd_id=issue['id'], queue=queue, origin_channel=scope['channel_id'],
                                  source_session=scope.get('session_id'), title=title, description=description,
                                  context=context, workspace=str(workspace), model=model, request_id=request_id, prerequisites=deps)
            return {'task': task, 'accepted': True}
        task = self.store.find(queue, self._text(body, 'task_id'))
        key = task['key']
        if command in ('show', 'result'):
            return self.store.detail(key)
        if command == 'tell':
            message_id = self.store.tell(key, self._text(body, 'text'))
            return {'message_id': message_id, 'delivery': 'queued',
                    'instruction': 'Worker receives this at its next tool boundary/inbox check. Use show to check acknowledgment; resume explicitly if stopped.'}
        if command == 'stop':
            self.store.stop(key, body.get('text') or 'Stopped by Wendy')
        elif command in ('resume', 'retry', 'model'):
            model = select_task_model(self._text(body, 'model')) if command == 'model' else None
            self.store.requeue(key, retry=command == 'retry', model=model)
        else:
            raise ValueError('Unknown command; run wtask --help')
        return {'task': self.store.get(key)}

    @staticmethod
    def _text(body, name):
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{name} must be a nonempty string')
        return value.strip()
