"""Background task runner (Beads).

Asyncio replacement for v1's orchestrator service. Polls beads task queues,
forks sessions, spawns Claude CLI agents, notifies on completion.

Lifecycle of a task:
  1. Wendy (or anyone) runs `bd create "title" -d "description"` in a channel.
  2. The poll loop sees it in `bd ready --unassigned` and claims it
     (status=in_progress, assignee=task-runner).
  3. A Claude CLI agent is spawned, forked from the channel's current session.
  4. The agent works, then closes its own task: `bd done <id> "summary"`.
  5. The runner notices the process exit, reads the close reason and the final
     result from the agent log, and writes a task_completion notification
     routed to the task's own channel.

Stuck-task recovery: any task left in_progress with no live agent (crash,
restart, spawn failure) is reopened by `_sweep_stuck_tasks` on the next poll,
as long as it was claimed by this runner (assignee=task-runner) or has no
assignee at all. Tasks a human explicitly claimed are left alone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

from .config import CLI_SUBPROCESS_UID, SENSITIVE_ENV_VARS, USAGE_BUDGET_FACTOR, parse_channel_configs, resolve_model
from .paths import WENDY_BASE, beads_dir, channel_dir, current_session_file, session_dir
from .state import state as state_manager

_LOG = logging.getLogger(__name__)

# Configuration
CONCURRENCY: int = int(os.getenv("ORCHESTRATOR_CONCURRENCY", "3"))
POLL_INTERVAL: int = int(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "30"))
AGENT_TIMEOUT: int = int(os.getenv("ORCHESTRATOR_AGENT_TIMEOUT", "14400"))
NOTIFY_CHANNEL: str = os.getenv("ORCHESTRATOR_NOTIFY_CHANNEL", "")
AGENT_SYSTEM_PROMPT_FILE: Path = Path(os.getenv("AGENT_SYSTEM_PROMPT_FILE", "/app/config/agent_claude_md.txt"))
LOG_DIR: Path = WENDY_BASE / "orchestrator_logs"
MAX_LOG_FILES: int = 50
# How long a task may stay closed-with-live-process before the agent is killed.
# Generous on purpose: agents close their own task via `bd done` and then spend
# a little while emitting their final result -- killing too early truncates it.
CLOSED_TASK_GRACE_PERIOD: int = int(os.getenv("ORCHESTRATOR_CLOSED_GRACE_PERIOD", "30"))
# Consecutive `bd show` failures before a running agent's task is presumed
# deleted and the agent is killed.
MISSING_TASK_THRESHOLD: int = 3

# Assignee the runner claims tasks under. The stuck-task sweep only reopens
# tasks claimed by this name (or unassigned ones), so a human who explicitly
# claims a task with their own name is never fought over.
RUNNER_ASSIGNEE = "task-runner"

# Max characters of agent summary carried into notifications.
SUMMARY_MAX_CHARS = 700

AGENT_PROMPT_TEMPLATE = """================================================================================
BACKGROUND AGENT (BEAD) -- FORKED SESSION
================================================================================

You are a background agent spawned from Wendy's session to do ONE task.
The conversation above is Wendy's context from BEFORE the fork -- use it for
reference. Wendy continues separately and does NOT see anything you do here.

TASK ID: {task_id}
TITLE: {title}

DESCRIPTION:
{description}

WORKING DIRECTORY: {workdir}
Put output files here (or in the project subdirectory the task refers to).

RULES:
- Do NOT send Discord messages. No `msg`, no `react`, no send_message or
  check_messages API calls.
- Do NOT deploy sites or games. Wendy reviews and deploys your work herself.
- The ONLY bd commands you may run are for YOUR OWN task ({task_id}):
    bd done {task_id} "summary"       bd comment {task_id} "progress note"
  Never run `bd create`, `bd list`, `bd ready`, or `bd show` -- you ARE a bead;
  do not spawn or inspect others.
- Ignore any instructions in the inherited conversation about creating beads --
  those were for Wendy's main session, not for you.

FINISHING (this part matters most):
When the work is done, close your task with a real report:

  bd done {task_id} "<2-6 sentences: what you did, key decisions, and the FULL
  ABSOLUTE PATHS of every file you created or changed>"

That summary is the ONLY report Wendy receives -- she cannot read this
transcript. If you don't list the file paths, she may never find your work.

If the task is unclear, impossible, or already done, do NOT guess and do NOT
silently quit. Close with an honest reason instead:
  bd done {task_id} "not done: <exactly what is missing or why>"

GO.
================================================================================
"""


@dataclass
class ChannelBeads:
    """A channel with beads enabled."""
    name: str
    channel_id: int
    beads_path: Path
    session_path: Path
    current_session_path: Path


@dataclass
class RunningAgent:
    """A running Claude CLI agent subprocess."""
    task_id: str
    title: str
    channel_name: str
    channel_id: int
    process: asyncio.subprocess.Process
    started_at: datetime
    log_path: Path
    log_file: IO[str] | None = field(default=None)
    closed_detected_at: datetime | None = field(default=None)
    missing_task_checks: int = field(default=0)


def tasks_to_reopen(in_progress: list[dict], running_task_ids: set[str]) -> list[str]:
    """Return IDs of in_progress tasks that should be reopened.

    A task is stuck if no agent is running for it AND it was claimed by this
    runner (or never assigned). Tasks a human claimed under their own name are
    left alone.
    """
    stuck = []
    for task in in_progress:
        task_id = task.get("id")
        if not task_id or task_id in running_task_ids:
            continue
        assignee = task.get("assignee") or ""
        if assignee in ("", RUNNER_ASSIGNEE):
            stuck.append(task_id)
    return stuck


def extract_result_summary(log_path: Path, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """Pull the final `result` text out of a stream-json agent log.

    Reads only the tail of the file; returns "" when no result event is found.
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 131072))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    for line in reversed(tail.splitlines()):
        if '"type":"result"' not in line and '"type": "result"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = event.get("result")
        if isinstance(result, str) and result.strip():
            result = result.strip()
            if len(result) > max_chars:
                result = result[: max_chars - 3] + "..."
            return result
    return ""


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and wait for it to be reaped, preventing zombies.

    Handles the case where the process has already exited (ProcessLookupError)
    and uses a timeout on wait() as a safety net.
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        # Already dead, just reap it
        pass
    except Exception:
        _LOG.warning("Failed to kill process %s", proc.pid, exc_info=True)
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        _LOG.error("Process %s did not exit within 10s after kill", proc.pid)
    except Exception:
        _LOG.warning("Error waiting for process %s", proc.pid, exc_info=True)


def _close_log_file(agent: RunningAgent) -> None:
    """Safely close an agent's log file."""
    if agent.log_file is not None:
        try:
            agent.log_file.close()
        except Exception:
            _LOG.warning("Failed to close log file for agent %s", agent.task_id)
        finally:
            agent.log_file = None


class TaskRunner:
    """Polls beads for tasks, spawns agents, monitors completion."""

    def __init__(self) -> None:
        self.agents: dict[str, RunningAgent] = {}
        self.beads_channels: list[ChannelBeads] = []
        self._last_usage_check: float = 0.0
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _load_beads_channels(self) -> list[ChannelBeads]:
        """Find channels with beads_enabled from config."""
        channels = []
        for channel_id, cfg in parse_channel_configs().items():
            if not cfg.get("beads_enabled"):
                continue
            name = cfg.get("_folder") or cfg.get("name")
            if not name:
                continue
            channels.append(ChannelBeads(
                name=name,
                channel_id=channel_id,
                beads_path=beads_dir(name),
                session_path=session_dir(name),
                current_session_path=current_session_file(name),
            ))
        return channels

    async def run(self) -> None:
        """Main polling loop. Runs as asyncio.create_task()."""
        self.beads_channels = self._load_beads_channels()
        if not self.beads_channels:
            _LOG.info("No beads-enabled channels, task runner idle")
            return

        _LOG.info("Task runner started: channels=%s concurrency=%d poll=%ds",
                  [c.name for c in self.beads_channels], CONCURRENCY, POLL_INTERVAL)

        # Init beads for channels that need it (check config.yaml, not just directory
        # existence -- ensure_channel_dirs creates .beads/ via mkdir but bd init
        # populates it with config.yaml, database, etc.)
        for channel in self.beads_channels:
            if not (channel.beads_path / "config.yaml").exists():
                await self._run_bd(["bd", "init"], channel)

        try:
            while True:
                try:
                    await self._check_agents()
                    await self._check_closed_tasks()
                    await self._sweep_stuck_tasks()

                    available = CONCURRENCY - len(self.agents)
                    if available > 0:
                        for channel in self.beads_channels:
                            if available <= 0:
                                break
                            tasks = await self._get_ready_tasks(channel)
                            for task in tasks:
                                task_id = task.get("id")
                                if task_id in self.agents:
                                    continue
                                if await self._claim_task(task_id, channel):
                                    agent = await self._spawn_agent(task, channel)
                                    if agent:
                                        self.agents[task_id] = agent
                                        available -= 1
                                        self._notify_started(agent)
                                    else:
                                        # Spawn failed -- release the claim so the
                                        # task can be retried later. If the release
                                        # also fails, _sweep_stuck_tasks catches it.
                                        await self._release_task(task_id, channel)
                                if available <= 0:
                                    break

                    self._cleanup_logs()
                    await self._write_beads_snapshot()
                    # Disabled: server token lacks user:profile scope, so every
                    # call fails and may still count toward account rate limit.
                    # await self._check_usage()
                except Exception:
                    _LOG.exception("Task runner loop error")

                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            _LOG.info("Task runner cancelled, cleaning up %d agents", len(self.agents))
            await self._shutdown_all_agents()
            raise

    async def _shutdown_all_agents(self) -> None:
        """Kill and clean up all running agents. Called on shutdown."""
        for task_id, agent in list(self.agents.items()):
            _LOG.info("Shutting down agent %s", task_id)
            await _kill_and_reap(agent.process)
            _close_log_file(agent)
            # Release so the task can be picked up on next startup
            channel = self._channel_by_name(agent.channel_name)
            if channel:
                try:
                    await self._release_task(task_id, channel)
                except Exception:
                    _LOG.warning("Failed to release task %s during shutdown", task_id)
        self.agents.clear()

    def _channel_by_name(self, name: str) -> ChannelBeads | None:
        for channel in self.beads_channels:
            if channel.name == name:
                return channel
        return None

    async def _run_bd(self, cmd: list[str], channel: ChannelBeads, timeout: int = 30) -> tuple[int, str, str]:
        """Run a bd command in a channel directory."""
        # Run as wendy user to match CLI subprocess permissions -- running as root
        # creates root-owned .beads/config.yaml that the CLI subprocess can't read.
        bd_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
        # Pin the database explicitly: cwd-based discovery can be hijacked by a
        # .beads/redirect file inside a cloned repo in the channel workspace.
        bd_env["BEADS_DIR"] = str(channel.beads_path)
        if CLI_SUBPROCESS_UID is not None:
            bd_env["HOME"] = "/home/wendy"
        user_kwargs = {"user": CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID else {}
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=channel_dir(channel.name),
                env=bd_env,
                **user_kwargs,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout.decode(), stderr.decode()
        except TimeoutError:
            _LOG.warning("bd command timed out: %s", cmd)
            if proc is not None:
                await _kill_and_reap(proc)
            return -1, "", "timeout"
        except FileNotFoundError:
            _LOG.error("bd command not found")
            return -1, "", "not found"

    async def _get_ready_tasks(self, channel: ChannelBeads) -> list[dict]:
        """Get ready, unassigned tasks from a channel's beads queue."""
        if not (channel.beads_path / "config.yaml").exists():
            return []
        code, stdout, stderr = await self._run_bd(
            ["bd", "ready", "--unassigned", "--sort", "priority", "--json"],
            channel,
        )
        if code != 0 or not stdout.strip():
            return []
        try:
            tasks = json.loads(stdout)
            for t in tasks:
                t["_channel_name"] = channel.name
            return tasks
        except json.JSONDecodeError:
            return []

    async def _claim_task(self, task_id: str, channel: ChannelBeads) -> bool:
        """Claim a task: in_progress + assigned to the runner.

        The assignee marks the claim as OURS so the stuck-task sweep can later
        distinguish runner-claimed tasks from ones a human is working on.
        """
        code, _, _ = await self._run_bd(
            ["bd", "update", task_id, "--status", "in_progress", "--assignee", RUNNER_ASSIGNEE],
            channel, timeout=10,
        )
        return code == 0

    async def _release_task(self, task_id: str, channel: ChannelBeads) -> None:
        """Reopen a task and clear the runner's claim so it can be re-picked."""
        await self._run_bd(
            ["bd", "update", task_id, "--status", "open", "--assignee", ""],
            channel, timeout=10,
        )

    async def _sweep_stuck_tasks(self) -> None:
        """Reopen in_progress tasks that have no live agent.

        Catches every way a claim can leak: bot crash mid-run, spawn failure
        whose release also failed, container kill without graceful shutdown.
        Without this, an orphaned in_progress task is invisible to
        `bd ready` forever and looks 'stuck' from Discord.
        """
        running = set(self.agents.keys())
        for channel in self.beads_channels:
            if not (channel.beads_path / "config.yaml").exists():
                continue
            code, stdout, _ = await self._run_bd(
                ["bd", "list", "--status", "in_progress", "--json"], channel, timeout=10,
            )
            if code != 0 or not stdout.strip():
                continue
            try:
                in_progress = json.loads(stdout)
            except json.JSONDecodeError:
                continue
            for task_id in tasks_to_reopen(in_progress, running):
                _LOG.warning("Reopening stuck task %s (in_progress, no live agent)", task_id)
                await self._release_task(task_id, channel)

    async def _get_task_details(self, task_id: str, channel: ChannelBeads) -> dict | None:
        """Get full task details."""
        code, stdout, _ = await self._run_bd(["bd", "show", task_id, "--json"], channel, timeout=10)
        if code != 0:
            return None
        try:
            data = json.loads(stdout)
            return data[0] if isinstance(data, list) and data else data
        except json.JSONDecodeError:
            return None

    async def _spawn_agent(self, task: dict, channel: ChannelBeads) -> RunningAgent | None:
        """Fork session and spawn a Claude CLI agent for a task."""
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled")
        channel_name = channel.name

        # Get full details
        details = await self._get_task_details(task_id, channel)
        description = (details or task).get("description", "")
        labels = (details or task).get("labels", [])

        # Parse model from labels (e.g., "model:opus")
        model = None
        for label in labels or []:
            if label.startswith("model:"):
                model = label.split(":", 1)[1]
                break
        model = resolve_model(model or "opus")

        # Note: str.format only interprets braces in the template itself, not in
        # substituted values -- title/description need no escaping.
        if not description.strip():
            description = ("(no description was provided -- work from the title and the "
                           "inherited conversation context; if that is not enough to act "
                           "confidently, close the task asking for a proper description)")
        prompt = AGENT_PROMPT_TEMPLATE.format(
            task_id=task_id, title=title, description=description,
            workdir=str(channel_dir(channel_name)),
        )

        # Create log file
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"agent_{task_id}_{ts}.log"

        try:
            # Check for session to fork from
            fork_session_id = None
            if channel.current_session_path.exists():
                try:
                    fork_session_id = channel.current_session_path.read_text().strip()
                    sess_file = channel.session_path / f"{fork_session_id}.jsonl"
                    if not sess_file.exists():
                        fork_session_id = None
                except Exception:
                    fork_session_id = None

            cmd = ["claude"]
            if fork_session_id:
                cmd.extend(["--resume", fork_session_id, "--fork-session"])
                _LOG.info("Forking from session %s for task %s", fork_session_id[:8], task_id)

            allowed_tools = (
                f"Read,WebSearch,WebFetch,Bash,Glob,Grep,TodoWrite,"
                f"Edit(//data/wendy/channels/{channel_name}/**),Write(//data/wendy/channels/{channel_name}/**),"
                f"Edit(//data/wendy/claude_fragments/people/**),Write(//data/wendy/claude_fragments/people/**),"
                f"Write(//data/wendy/tmp/**),Write(//tmp/**)"
            )
            disallowed_tools = "Edit(//app/**),Write(//app/**),Skill,TodoRead"

            cmd.extend([
                "-p", prompt,
                "--max-turns", "9999",
                "--strict-mcp-config",
                "--allowedTools", allowed_tools,
                "--disallowedTools", disallowed_tools,
                "--output-format", "stream-json",
                "--verbose",
                "--model", model,
            ])

            # Append agent system prompt if available
            if AGENT_SYSTEM_PROMPT_FILE.exists():
                try:
                    context = AGENT_SYSTEM_PROMPT_FILE.read_text().strip()
                    if context:
                        cmd.extend(["--append-system-prompt", context])
                except Exception:
                    pass

            # Build env for CLI subprocess isolation
            agent_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
            # Pin bd to this channel's database. Without this, an agent that
            # cd's into a cloned repo containing .beads/redirect would silently
            # talk to the wrong (or nonexistent) database and its `bd done`
            # would never close the real task.
            agent_env["BEADS_DIR"] = str(channel.beads_path)
            # Pass auth and sync tokens explicitly so the CLI can authenticate even though
            # they're stripped from the general env (to keep them out of `env` output).
            if oauth_token := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
                agent_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
            if sync_key := os.environ.get("CLAUDE_SYNC_KEY"):
                agent_env["CLAUDE_SYNC_KEY"] = sync_key
            if CLI_SUBPROCESS_UID is not None:
                agent_env["HOME"] = "/home/wendy"
            user_kwargs = {"user": CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID else {}

            log_file = open(log_path, "w")
            try:
                log_file.write(f"Task: {task_id} - {title}\n")
                log_file.write(f"Channel: {channel_name}\n")
                log_file.write(f"Model: {model}\n")
                log_file.write(f"Started: {datetime.now().isoformat()}\n")
                log_file.write("=" * 60 + "\n\n")
                log_file.flush()

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=channel_dir(channel_name),
                    env=agent_env,
                    **user_kwargs,
                )
            except Exception:
                log_file.close()
                raise

            _LOG.info("Spawned agent for task %s: %s (model=%s)", task_id, title, model)
            return RunningAgent(
                task_id=task_id,
                title=title,
                channel_name=channel_name,
                channel_id=channel.channel_id,
                process=proc,
                started_at=datetime.now(),
                log_path=log_path,
                log_file=log_file,
            )

        except Exception:
            _LOG.exception("Failed to spawn agent for task %s", task_id)
            return None

    async def _cleanup_agent(self, agent: RunningAgent, *, kill: bool = False) -> None:
        """Kill (if requested) and clean up a single agent's resources.

        Always closes the log file and reaps the process to prevent zombies.
        """
        if kill:
            await _kill_and_reap(agent.process)
        elif agent.process.returncode is None:
            # Process should already be done, but reap it just in case
            try:
                await asyncio.wait_for(agent.process.wait(), timeout=5)
            except TimeoutError:
                _LOG.warning("Agent %s process still alive after completion detected, killing",
                             agent.task_id)
                await _kill_and_reap(agent.process)
        _close_log_file(agent)

    async def _finish_agent(self, agent: RunningAgent) -> None:
        """Handle a completed (exited) agent: report honestly what happened.

        The agent is supposed to close its own task via `bd done <id> "summary"`;
        that summary (the close reason) is the primary report. Fall back to the
        final result text from the stream-json log. A clean exit with the task
        still open is NOT reported as success -- that is exactly the 'agent
        ended the task without doing any work' failure mode.
        """
        exit_code = agent.process.returncode
        duration = datetime.now() - agent.started_at
        await self._cleanup_agent(agent)

        channel = self._channel_by_name(agent.channel_name)
        details = await self._get_task_details(agent.task_id, channel) if channel else None
        task_status = (details or {}).get("status", "")
        close_reason = ((details or {}).get("close_reason") or "").strip()
        log_result = extract_result_summary(agent.log_path)

        if task_status == "closed" and close_reason:
            summary = close_reason
            if exit_code == 0:
                status = "completed"
            else:
                status = "completed"
                summary += f" (note: agent process then exited with code {exit_code})"
        elif exit_code == 0:
            # Agent exited cleanly but never closed its task. Close it here so
            # it doesn't go stale, but report the gap instead of claiming success.
            if channel:
                await self._run_bd(
                    ["bd", "close", agent.task_id, "-r", "agent exited without closing the task"],
                    channel,
                )
            status = "finished WITHOUT closing its task (verify the work before trusting it)"
            summary = log_result or "no summary available -- check the task output manually"
        else:
            if channel and task_status != "closed":
                await self._run_bd(
                    ["bd", "close", agent.task_id, "-r", f"agent process exited with code {exit_code}"],
                    channel,
                )
            status = f"failed (exit code {exit_code})"
            summary = log_result

        _LOG.info("Agent %s %s after %s", agent.task_id, status, duration)
        self._notify_completion(agent, status, str(duration).split(".")[0], summary)

    async def _check_agents(self) -> None:
        """Check running agents for completion or timeout."""
        finished = []
        for task_id, agent in self.agents.items():
            duration = datetime.now() - agent.started_at
            secs = duration.total_seconds()

            # Check timeout -- process is still alive but exceeded the time limit
            if agent.process.returncode is None and secs > AGENT_TIMEOUT:
                _LOG.warning("Agent %s timed out after %s", task_id, duration)
                await self._cleanup_agent(agent, kill=True)
                channel = self._channel_by_name(agent.channel_name)
                if channel:
                    await self._run_bd(
                        ["bd", "close", task_id, "-r", f"agent timed out after {duration}"],
                        channel,
                    )
                self._notify_completion(agent, "timed out", str(duration).split(".")[0],
                                        extract_result_summary(agent.log_path))
                finished.append(task_id)
                continue

            # Check completion -- process has exited
            if agent.process.returncode is not None:
                await self._finish_agent(agent)
                finished.append(task_id)

        for tid in finished:
            del self.agents[tid]

    async def _check_closed_tasks(self) -> None:
        """Kill agents whose tasks were closed or deleted externally.

        This is the cancellation path: Wendy runs `bd close <id> -r "reason"`
        and the agent is killed after a grace period. The grace period also
        protects agents that just closed their OWN task and are still writing
        their final output -- _check_agents handles them once they exit.
        """
        to_kill = []
        for task_id, agent in self.agents.items():
            if agent.process.returncode is not None:
                # Will be cleaned up by _check_agents on next poll
                agent.closed_detected_at = None
                continue

            channel = self._channel_by_name(agent.channel_name)
            if channel is None:
                continue
            details = await self._get_task_details(task_id, channel)

            if details is None:
                # bd show failed -- could be lock contention, could be a deleted
                # task. Only act after several consecutive misses.
                agent.missing_task_checks += 1
                if agent.missing_task_checks >= MISSING_TASK_THRESHOLD:
                    to_kill.append((task_id, "task no longer exists (deleted?)"))
                continue
            agent.missing_task_checks = 0

            if details.get("status") == "closed":
                now = datetime.now()
                if agent.closed_detected_at is None:
                    agent.closed_detected_at = now
                    _LOG.info("Task %s closed while agent running, grace period %ds",
                              task_id, CLOSED_TASK_GRACE_PERIOD)
                elif (now - agent.closed_detected_at).total_seconds() >= CLOSED_TASK_GRACE_PERIOD:
                    reason = (details.get("close_reason") or "").strip()
                    to_kill.append((task_id, reason))

        for task_id, reason in to_kill:
            agent = self.agents[task_id]
            _LOG.info("Killing agent for externally-closed task %s", task_id)
            await self._cleanup_agent(agent, kill=True)
            duration = datetime.now() - agent.started_at
            summary = f"close reason: {reason}" if reason else ""
            self._notify_completion(agent, "cancelled (task closed while agent was running; agent killed)",
                                    str(duration).split(".")[0], summary)
            del self.agents[task_id]

    def _resolve_notify_channel(self, agent: RunningAgent) -> int | None:
        """Route notifications to the task's own channel unless overridden."""
        if NOTIFY_CHANNEL:
            try:
                return int(NOTIFY_CHANNEL)
            except ValueError:
                pass
        return agent.channel_id or None

    def _notify_started(self, agent: RunningAgent) -> None:
        """Write a low-key start notification so Wendy can answer 'is it running?'."""
        try:
            state_manager.add_notification(
                type="task_started",
                source="task_runner",
                title=agent.title,
                channel_id=self._resolve_notify_channel(agent),
                payload={"task_id": agent.task_id},
            )
        except Exception:
            _LOG.exception("Failed to write start notification for %s", agent.task_id)

    def _notify_completion(self, agent: RunningAgent, status: str, duration: str, summary: str = "") -> None:
        """Write completion notification to SQLite, routed to the task's channel."""
        try:
            state_manager.add_notification(
                type="task_completion",
                source="task_runner",
                title=agent.title,
                channel_id=self._resolve_notify_channel(agent),
                payload={
                    "task_id": agent.task_id,
                    "status": status,
                    "duration": duration,
                    "summary": summary,
                },
            )
            state_manager.cleanup_old_notifications(keep_count=100)
        except Exception:
            _LOG.exception("Failed to write completion notification for %s", agent.task_id)

    async def _write_beads_snapshot(self) -> None:
        """Write a combined beads snapshot for the web dashboard.

        The web service can't run bd (it's not installed there), so we write
        a JSON file to the shared volume every poll cycle.
        """
        snapshot_path = WENDY_BASE / "shared" / "beads_snapshot.json"
        all_beads: list[dict] = []
        try:
            for channel in self.beads_channels:
                if not (channel.beads_path / "config.yaml").exists():
                    continue
                code, stdout, _ = await self._run_bd(
                    ["bd", "list", "--json"], channel, timeout=10,
                )
                if code != 0 or not stdout.strip():
                    continue
                try:
                    issues = json.loads(stdout)
                    for issue in issues:
                        issue["_channel"] = channel.name
                    all_beads.extend(issues)
                except json.JSONDecodeError:
                    continue
            snapshot_path.write_text(json.dumps(all_beads))
        except Exception:
            _LOG.debug("Failed to write beads snapshot", exc_info=True)

    async def _check_usage(self) -> None:
        """Periodically check Claude Code usage via get_usage.sh."""
        usage_poll_interval = 3600  # 1 hour
        usage_script = Path("/app/scripts/get_usage.sh")
        usage_data_file = WENDY_BASE / "usage_data.json"
        force_check_file = WENDY_BASE / "usage_force_check"

        now = time.time()
        force = force_check_file.exists()
        if force:
            try:
                force_check_file.unlink()
            except Exception:
                pass

        if not force and now - self._last_usage_check < usage_poll_interval:
            return
        self._last_usage_check = now

        if not usage_script.exists():
            return

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(usage_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=WENDY_BASE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                return

            usage = json.loads(stdout.decode())
            if USAGE_BUDGET_FACTOR < 1.0:
                for key in ("week_all_percent", "week_sonnet_percent", "session_percent"):
                    if key in usage:
                        usage[key] = min(100, int(usage[key] / USAGE_BUDGET_FACTOR))
            usage["updated_at"] = datetime.now().isoformat()
            usage_data_file.write_text(json.dumps(usage, indent=2))
            _LOG.info("Usage: week_all=%s%%, week_sonnet=%s%%",
                      usage.get("week_all_percent", 0), usage.get("week_sonnet_percent", 0))
        except TimeoutError:
            _LOG.warning("Usage check timed out")
            if proc is not None:
                await _kill_and_reap(proc)
        except Exception:
            _LOG.warning("Usage check failed", exc_info=True)

    def _cleanup_logs(self) -> None:
        """Trim old agent log files."""
        try:
            logs = sorted(LOG_DIR.glob("agent_*.log"), key=lambda f: f.stat().st_mtime)
            for old in logs[:-MAX_LOG_FILES]:
                old.unlink()
        except Exception:
            pass
