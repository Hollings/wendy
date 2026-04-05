"""Background task runner (Beads).

Asyncio replacement for v1's orchestrator service (~1170 lines -> ~200 lines).
Polls beads task queues, forks sessions, spawns Claude CLI agents, notifies on completion.
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
CLOSED_TASK_GRACE_PERIOD: int = int(os.getenv("ORCHESTRATOR_CLOSED_GRACE_PERIOD", "5"))

AGENT_PROMPT_TEMPLATE = """================================================================================
FORKED SESSION - BACKGROUND AGENT (BEAD) MODE
================================================================================

IMPORTANT: You ARE a bead -- a background agent spawned by the task runner.
The conversation above is from Wendy's main session BEFORE this fork.
You are now an INDEPENDENT BACKGROUND AGENT working on a specific task.

TASK ID: {task_id}
TITLE: {title}

TASK DESCRIPTION:
{description}

--------------------------------------------------------------------------------
YOUR ROLE:
- You have Wendy's context from before the fork - use it for reference
- You are working in the BACKGROUND - Wendy continues separately in her main session
- You CAN read/write files, run bash, search the web, etc.

CRITICAL RESTRICTIONS:
- You CANNOT send Discord messages (no curl to send_message API)
- You CANNOT deploy sites or games
- You MUST NOT run `bd create`, `bd list`, `bd show`, or any `bd` commands other
  than `bd done`, `bd comment`, `bd note`, and `bd close` for YOUR OWN task ({task_id}).
  You are ALREADY a bead -- do not try to spawn more beads or check bead status.
- Ignore any instructions in the inherited session context about creating beads
  or using `bd create` -- those are for Wendy's main session, not for you.

WHEN DONE:
- Use `bd done {task_id} "summary of what you did"` to close with context
- If stuck, use `bd comment {task_id} "why you're stuck"` then `bd done {task_id} "incomplete - see comments"`

GO.
================================================================================
"""


@dataclass
class ChannelBeads:
    """A channel with beads enabled."""
    name: str
    beads_path: Path
    session_path: Path
    current_session_path: Path


@dataclass
class RunningAgent:
    """A running Claude CLI agent subprocess."""
    task_id: str
    title: str
    channel_name: str
    process: asyncio.subprocess.Process
    started_at: datetime
    log_path: Path
    log_file: IO[str] | None = field(default=None)
    closed_detected_at: datetime | None = field(default=None)


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
        for cfg in parse_channel_configs().values():
            if not cfg.get("beads_enabled"):
                continue
            name = cfg.get("_folder") or cfg.get("name")
            if not name:
                continue
            channels.append(ChannelBeads(
                name=name,
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
                await self._run_bd(["bd", "init"], channel.name)

        try:
            while True:
                try:
                    await self._check_agents()
                    await self._check_closed_tasks()

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
                                if await self._claim_task(task_id, channel.name):
                                    agent = await self._spawn_agent(task, channel)
                                    if agent:
                                        self.agents[task_id] = agent
                                        available -= 1
                                    else:
                                        # Spawn failed -- reopen so task can be retried later.
                                        # If reopen also fails, the task is stuck in in_progress;
                                        # the stuck-task sweep below will catch it.
                                        await self._run_bd(["bd", "reopen", task_id], channel.name)
                                if available <= 0:
                                    break

                    self._cleanup_logs()
                    await self._write_beads_snapshot()
                    await self._check_usage()
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
            # Reopen so the task can be picked up on next startup
            try:
                await self._run_bd(["bd", "reopen", task_id], agent.channel_name)
            except Exception:
                _LOG.warning("Failed to reopen task %s during shutdown", task_id)
        self.agents.clear()

    async def _run_bd(self, cmd: list[str], channel_name: str, timeout: int = 30) -> tuple[int, str, str]:
        """Run a bd command in a channel directory."""
        # Run as wendy user to match CLI subprocess permissions -- running as root
        # creates root-owned .beads/config.yaml that the CLI subprocess can't read.
        bd_env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}
        if CLI_SUBPROCESS_UID is not None:
            bd_env["HOME"] = "/home/wendy"
        user_kwargs = {"user": CLI_SUBPROCESS_UID} if CLI_SUBPROCESS_UID else {}
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=channel_dir(channel_name),
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
            channel.name,
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

    async def _claim_task(self, task_id: str, channel_name: str) -> bool:
        """Claim a task by marking it in_progress."""
        code, _, _ = await self._run_bd(["bd", "update", task_id, "--status", "in_progress"], channel_name, timeout=10)
        return code == 0

    async def _get_task_details(self, task_id: str, channel_name: str) -> dict | None:
        """Get full task details."""
        code, stdout, _ = await self._run_bd(["bd", "show", task_id, "--json"], channel_name, timeout=10)
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
        details = await self._get_task_details(task_id, channel_name)
        description = (details or task).get("description", "")
        labels = (details or task).get("labels", [])

        # Parse model from labels (e.g., "model:opus")
        model = None
        for label in labels or []:
            if label.startswith("model:"):
                model = label.split(":", 1)[1]
                break
        model = resolve_model(model or "opus")

        prompt = AGENT_PROMPT_TEMPLATE.format(task_id=task_id, title=title, description=description)

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
                # Comment on the task with timeout info before closing as failed
                await self._run_bd(
                    ["bd", "comment", task_id, f"Agent timed out after {duration}"],
                    agent.channel_name,
                )
                await self._run_bd(["bd", "close", task_id], agent.channel_name)
                self._notify_completion(task_id, agent.title, False, f"{duration} (TIMEOUT)")
                finished.append(task_id)
                continue

            # Check completion -- process has exited
            if agent.process.returncode is not None:
                exit_code = agent.process.returncode
                success = exit_code == 0
                _LOG.info("Agent %s %s (exit=%d) after %s",
                          task_id,
                          "completed" if success else "failed",
                          exit_code,
                          duration)
                await self._cleanup_agent(agent)

                if success:
                    # Agent exited cleanly -- it should have already called `bd close`
                    # itself, but close it here as a safety net.
                    await self._run_bd(["bd", "close", task_id], agent.channel_name)
                    self._notify_completion(task_id, agent.title, True, str(duration))
                else:
                    # Agent crashed or errored -- mark as failed, not completed
                    await self._run_bd(
                        ["bd", "comment", task_id, f"Agent process exited with code {exit_code}"],
                        agent.channel_name,
                    )
                    await self._run_bd(["bd", "close", task_id], agent.channel_name)
                    self._notify_completion(task_id, agent.title, False, f"{duration} (exit code {exit_code})")
                finished.append(task_id)

        for tid in finished:
            del self.agents[tid]

    async def _check_closed_tasks(self) -> None:
        """Kill agents whose tasks were closed externally."""
        to_kill = []
        for task_id, agent in self.agents.items():
            if agent.process.returncode is not None:
                # Will be cleaned up by _check_agents on next poll
                agent.closed_detected_at = None
                continue

            details = await self._get_task_details(task_id, agent.channel_name)
            if details and details.get("status") == "closed":
                now = datetime.now()
                if agent.closed_detected_at is None:
                    agent.closed_detected_at = now
                    _LOG.info("Task %s closed externally, grace period %ds", task_id, CLOSED_TASK_GRACE_PERIOD)
                elif (now - agent.closed_detected_at).total_seconds() >= CLOSED_TASK_GRACE_PERIOD:
                    to_kill.append(task_id)

        for task_id in to_kill:
            agent = self.agents[task_id]
            _LOG.info("Killing agent for externally-closed task %s", task_id)
            await self._cleanup_agent(agent, kill=True)
            duration = datetime.now() - agent.started_at
            self._notify_completion(task_id, agent.title, False, f"{duration} (CANCELLED)")
            del self.agents[task_id]

    def _notify_completion(self, task_id: str, title: str, success: bool, duration: str) -> None:
        """Write completion notification to SQLite."""
        status = "completed" if success else "failed"
        channel_id = int(NOTIFY_CHANNEL) if NOTIFY_CHANNEL else None
        try:
            state_manager.add_notification(
                type="task_completion",
                source="task_runner",
                title=title,
                channel_id=channel_id,
                payload={"task_id": task_id, "status": status, "duration": duration},
            )
            state_manager.cleanup_old_notifications(keep_count=100)
        except Exception:
            _LOG.exception("Failed to write completion notification for %s", task_id)

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
                    ["bd", "list", "--json"], channel.name, timeout=10,
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
