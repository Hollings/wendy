"""Bead Orchestrator - Autonomous task execution system using Claude Code agents.

This module implements a task orchestrator that:
- Polls the beads task queue for ready, unassigned tasks
- Spawns Claude Code CLI agents to work on tasks autonomously
- Manages concurrent agent execution up to a configurable limit
- Handles agent timeouts, cancellations, and completions
- Sends Discord notifications for task status updates
- Monitors Claude Code subscription usage and alerts on thresholds

Architecture:
    The orchestrator runs as a standalone process that coordinates with:
    - Beads (`bd` CLI): Task queue management (ready, claim, close)
    - Claude Code CLI: Agent execution with streaming output
    - Discord Proxy: Notifications via HTTP API
    - Usage Script: Claude subscription usage monitoring

Example:
    Run the orchestrator:
        $ python -m orchestrator.main

    Or import and run programmatically:
        >>> from orchestrator.main import Orchestrator
        >>> orchestrator = Orchestrator()
        >>> orchestrator.run()
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# =============================================================================
# Configuration Constants
# =============================================================================

CONCURRENCY: int = int(os.getenv("ORCHESTRATOR_CONCURRENCY", "1"))
"""Maximum number of concurrent Claude Code agents to run.

Set via ORCHESTRATOR_CONCURRENCY environment variable. Default is 1 to avoid
overwhelming the system. Higher values (2-4) can be used if tasks are I/O bound.
"""

POLL_INTERVAL: int = int(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "30"))
"""Seconds between polling for new tasks. Default 30 seconds."""

WORKING_DIR: Path = Path(os.getenv("ORCHESTRATOR_WORKING_DIR", "/data/wendy"))
"""Base working directory for orchestrator data and outputs."""

BEADS_DIR: Path = WORKING_DIR / "coding"
"""Directory containing the .beads/ task queue (coding channel folder)."""

CURRENT_SESSION_FILE: Path = BEADS_DIR / ".current_session"
"""File containing Wendy's current session ID for forking."""

SESSION_DIR: Path = Path(os.getenv(
    "CLAUDE_SESSION_DIR",
    "/root/.claude/projects/-data-wendy-coding"
))
"""Directory where Claude CLI session JSONL files are stored.

By default, Claude CLI stores sessions in ~/.claude/projects/<encoded-path>/
where the working directory path is encoded (e.g., /data/wendy/coding -> -data-wendy-coding).
Override with CLAUDE_SESSION_DIR environment variable if needed.
"""

LOG_DIR: Path = WORKING_DIR / "orchestrator_logs"
"""Directory for agent execution log files."""

PROXY_URL: str = os.getenv("ORCHESTRATOR_PROXY_URL", "http://127.0.0.1:8945")
"""URL of the Discord proxy API for sending notifications."""

NOTIFY_CHANNEL: str = os.getenv("ORCHESTRATOR_NOTIFY_CHANNEL", "")
"""Discord channel ID to send task completion notifications to."""

AGENT_TIMEOUT: int = int(os.getenv("ORCHESTRATOR_AGENT_TIMEOUT", "1800"))
"""Maximum seconds an agent can run before being terminated. Default 30 minutes."""

MAX_LOG_FILES: int = int(os.getenv("ORCHESTRATOR_MAX_LOG_FILES", "50"))
"""Maximum number of log files to retain. Older logs are automatically cleaned up."""

AGENT_SYSTEM_PROMPT_FILE: Path = Path(os.getenv("AGENT_SYSTEM_PROMPT_FILE", "/app/config/agent_claude_md.txt"))
"""Path to additional system prompt context to append to agent prompts."""

CANCEL_FILE: Path = WORKING_DIR / "cancel_tasks.json"
"""JSON file containing task IDs that should be cancelled."""

# =============================================================================
# Usage Monitoring Configuration
# =============================================================================

USAGE_POLL_INTERVAL: int = int(os.getenv("ORCHESTRATOR_USAGE_POLL_INTERVAL", "3600"))
"""Seconds between Claude usage checks. Default 1 hour."""

USAGE_NOTIFY_CHANNEL: str = os.getenv("ORCHESTRATOR_USAGE_NOTIFY_CHANNEL", "")
"""Discord channel for usage threshold alerts. Falls back to NOTIFY_CHANNEL if empty."""

USAGE_STATE_FILE: Path = WORKING_DIR / "usage_state.json"
"""JSON file tracking last notified usage thresholds to avoid duplicate alerts."""

USAGE_DATA_FILE: Path = WORKING_DIR / "usage_data.json"
"""JSON file with latest usage data for the proxy dashboard to read."""

USAGE_FORCE_CHECK_FILE: Path = WORKING_DIR / "usage_force_check"
"""Sentinel file - if exists, triggers immediate usage check regardless of interval."""

USAGE_SCRIPT_PATH: Path = Path("/app/scripts/get_usage.sh")
"""Path to shell script that fetches Claude Code usage statistics."""

AGENT_PROMPT_TEMPLATE: str = """================================================================================
FORKED SESSION - BACKGROUND AGENT MODE
================================================================================

The conversation above is from Wendy's session BEFORE this fork.
You are now a BACKGROUND AGENT working on a specific task.

TASK ID: {task_id}
TITLE: {title}

TASK DESCRIPTION:
{description}

--------------------------------------------------------------------------------
YOUR ROLE:
- You have Wendy's context from before the fork - use it
- You are working in the BACKGROUND - Wendy continues separately
- You CANNOT send Discord messages or deploy
- You CAN read/write files, run bash, etc.

WHEN DONE:
- Use `bd comment {task_id} "your notes"` to leave context for Wendy
- Run `bd close {task_id}` when successfully completed
- If stuck, leave a comment explaining why

GO.
================================================================================
"""
"""Template for agent task prompts.

Placeholders:
    {task_id}: The beads task ID (e.g., "abc123")
    {title}: Task title/summary
    {description}: Full task description from beads

The template instructs agents to:
- Complete the task in the /data/wendy/coding/ directory
- Test changes locally if applicable
- NOT deploy anything (Wendy handles deployment)
"""

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("orchestrator")


@dataclass
class RunningAgent:
    """Tracks state for a running Claude Code agent subprocess.

    Each running agent corresponds to a beads task being worked on by
    an autonomous Claude Code CLI process. The orchestrator creates one
    of these for each spawned agent and uses it to monitor status,
    enforce timeouts, and clean up on completion.

    Attributes:
        task_id: The beads task ID (e.g., "abc123").
        title: Human-readable task title for logging and notifications.
        process: The subprocess.Popen object for the Claude CLI process.
        started_at: When the agent was spawned (for timeout tracking).
        log_file: Path to the log file capturing agent stdout/stderr.
    """

    task_id: str
    title: str
    process: subprocess.Popen
    started_at: datetime
    log_file: Path


class Orchestrator:
    """Main orchestrator for autonomous task execution using Claude Code agents.

    The orchestrator polls the beads task queue for ready, unassigned tasks
    and spawns Claude Code CLI agents to work on them. It manages concurrent
    execution, handles timeouts and cancellations, and sends Discord notifications
    for status updates.

    Attributes:
        active_agents: Map of task_id to RunningAgent for in-progress work.
        concurrency: Maximum number of concurrent agents allowed.
        last_usage_check: Timestamp of last Claude usage check (for rate limiting).

    Example:
        >>> orchestrator = Orchestrator()
        >>> orchestrator.run()  # Starts the main polling loop
    """

    def __init__(self) -> None:
        """Initialize the orchestrator.

        Creates the log directory if needed and sets up initial state.
        Does not start the main loop - call run() for that.
        """
        self.active_agents: dict[str, RunningAgent] = {}
        self.concurrency: int = CONCURRENCY
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.last_usage_check: float = 0.0

    def _send_discord_message(self, channel: str, content: str) -> bool:
        """Send a message to Discord via the proxy API.

        Args:
            channel: Discord channel ID to send to.
            content: Message content (supports Discord markdown).

        Returns:
            True if message was sent successfully, False on any error.
        """
        try:
            data = json.dumps({
                "channel_id": channel,
                "content": content
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{PROXY_URL}/api/send_message",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200

        except urllib.error.URLError as e:
            log.warning(f"Failed to send Discord message: {e}")
            return False
        except Exception as e:
            log.error(f"Discord message error: {e}")
            return False

    def _terminate_agent(self, agent: RunningAgent, reason: str) -> None:
        """Terminate an agent process and append reason to its log file.

        Attempts graceful termination first (SIGTERM), then forceful kill
        (SIGKILL) if the process doesn't exit within 5 seconds.

        Args:
            agent: The RunningAgent to terminate.
            reason: Human-readable reason for termination (logged to file).
        """
        try:
            agent.process.terminate()
            agent.process.wait(timeout=5)
        except Exception:
            agent.process.kill()

        with open(agent.log_file, "a") as f:
            f.write("\n" + "=" * 60 + "\n")
            f.write(f"{reason}\n")
            f.write(f"Completed: {datetime.now().isoformat()}\n")

    def get_ready_tasks(self) -> list[dict]:
        """Get list of ready, unassigned tasks from beads, sorted by priority.

        Runs `bd ready --unassigned --sort priority --json` to fetch tasks
        that are ready to be worked on and haven't been claimed yet.

        Returns:
            List of task dicts with keys like 'id', 'title', 'priority', 'labels'.
            Returns empty list on any error (command failure, parse error, timeout).
        """
        try:
            result = subprocess.run(
                ["bd", "ready", "--unassigned", "--sort", "priority", "--json"],
                capture_output=True,
                text=True,
                cwd=BEADS_DIR,
                timeout=30
            )
            if result.returncode != 0:
                log.warning(f"bd ready failed: {result.stderr}")
                return []

            if not result.stdout.strip():
                return []

            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            log.error("bd ready timed out")
            return []
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse bd ready output: {e}")
            return []
        except FileNotFoundError:
            log.error("bd command not found - is beads installed?")
            return []

    def claim_task(self, task_id: str) -> bool:
        """Atomically claim a task by marking it as in-progress.

        Runs `bd update <task_id> --claim` which sets the task assignee
        and prevents other orchestrators from picking it up.

        Args:
            task_id: The beads task ID to claim.

        Returns:
            True if claim succeeded, False on any error.
        """
        try:
            result = subprocess.run(
                ["bd", "update", task_id, "--claim"],
                capture_output=True,
                text=True,
                cwd=BEADS_DIR,
                timeout=10
            )
            if result.returncode != 0:
                log.warning(f"Failed to claim {task_id}: {result.stderr}")
            return result.returncode == 0
        except Exception as e:
            log.error(f"Failed to claim task {task_id}: {e}")
            return False

    def complete_task(self, task_id: str, success: bool = True) -> bool:
        """Mark a task as complete or failed in beads.

        Args:
            task_id: The beads task ID.
            success: If True, runs `bd close`; if False, runs `bd reopen`.

        Returns:
            True if the status update succeeded, False on error.
        """
        try:
            cmd = ["bd", "close" if success else "reopen", task_id]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=BEADS_DIR,
                timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            log.error(f"Failed to complete task {task_id}: {e}")
            return False

    def get_task_details(self, task_id: str) -> dict | None:
        """Fetch full task details from beads.

        Runs `bd show <task_id> --json` to get the complete task record
        including description and labels.

        Args:
            task_id: The beads task ID.

        Returns:
            Task dict with full details, or None if task not found or on error.
        """
        try:
            result = subprocess.run(
                ["bd", "show", task_id, "--json"],
                capture_output=True,
                text=True,
                cwd=BEADS_DIR,
                timeout=10
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                # bd show returns an array with one element
                if isinstance(data, list) and data:
                    return data[0]
                return data
        except Exception as e:
            log.debug(f"Failed to get task details: {e}")
        return None

    def parse_model_from_labels(self, labels: list | None) -> str | None:
        """Extract model specification from task labels.

        Looks for labels in the format 'model:<model_name>' and extracts
        the model name for passing to Claude CLI.

        Args:
            labels: List of task labels from beads.

        Returns:
            Model name (e.g., 'opus', 'sonnet') or None if not specified.

        Example:
            >>> self.parse_model_from_labels(['priority:high', 'model:opus'])
            'opus'
        """
        for label in labels or []:
            if label.startswith("model:"):
                return label.split(":", 1)[1]
        return None

    def spawn_agent(self, task: dict) -> RunningAgent | None:
        """Spawn a Claude Code CLI agent to work on a task.

        Creates a log file for the agent, builds the CLI command with
        appropriate flags, and spawns the subprocess. The agent runs
        autonomously until completion, timeout, or cancellation.

        Args:
            task: Task dict from beads with keys: id, title, description,
                priority, labels.

        Returns:
            RunningAgent instance for tracking, or None if spawn failed.

        Note:
            Uses --allowedTools instead of --dangerously-skip-permissions
            because the latter doesn't work when running as root.
        """
        task_id = task.get("id", "unknown")
        title = task.get("title", "Untitled task")
        description = task.get("description", "")
        priority = task.get("priority", 2)
        labels = task.get("labels", [])

        # Get more details if available
        details = self.get_task_details(task_id)
        if details:
            description = details.get("description", description)
            labels = details.get("labels", labels)

        # Parse model from labels (e.g., "model:opus" -> "opus")
        model = self.parse_model_from_labels(labels)

        # Build the prompt for the agent
        prompt = AGENT_PROMPT_TEMPLATE.format(
            task_id=task_id,
            title=title,
            description=description
        )

        # Create log file for this agent
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = LOG_DIR / f"agent_{task_id}_{timestamp}.log"

        try:
            with open(log_file, "w") as f:
                f.write(f"Task: {task_id} - {title}\n")
                f.write(f"Priority: P{priority}\n")
                f.write(f"Model: {model or 'default'}\n")
                f.write(f"Started: {datetime.now().isoformat()}\n")
                f.write(f"Prompt:\n{prompt}\n")
                f.write("=" * 60 + "\n\n")

            # Check if we can fork from Wendy's session
            fork_session_id = None
            if CURRENT_SESSION_FILE.exists():
                try:
                    fork_session_id = CURRENT_SESSION_FILE.read_text().strip()
                    # Verify session file exists
                    session_file = SESSION_DIR / f"{fork_session_id}.jsonl"
                    if not session_file.exists():
                        log.warning(f"Session {fork_session_id[:8]} not found, using fresh agent")
                        fork_session_id = None
                    else:
                        log.info(f"Forking from Wendy's session {fork_session_id[:8]} for task {task_id}")
                except Exception as e:
                    log.warning(f"Failed to read session file: {e}")
                    fork_session_id = None

            # Build CLI command
            # Note: Can't use --dangerously-skip-permissions or --permission-mode bypassPermissions
            # when running as root. Use --allowedTools to whitelist required tools instead.
            # -p = print mode (non-interactive), prompt is positional arg
            # --verbose required for stream-json output
            cmd = ["claude"]

            # Add session forking if available
            if fork_session_id:
                cmd.extend(["--resume", fork_session_id, "--fork-session"])
            else:
                log.info(f"No session to fork from, spawning fresh agent for task {task_id}")

            # Common arguments for all agents
            cmd.extend([
                "-p", prompt,
                "--max-turns", "9999",
                "--allowedTools", "Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite",
                "--output-format", "stream-json",
                "--verbose"
            ])

            # Add agent system prompt if available
            if AGENT_SYSTEM_PROMPT_FILE.exists():
                try:
                    agent_context = AGENT_SYSTEM_PROMPT_FILE.read_text().strip()
                    if agent_context:
                        cmd.extend(["--append-system-prompt", agent_context])
                except Exception as e:
                    log.warning(f"Failed to read agent system prompt: {e}")

            # Add model if specified
            if model:
                cmd.extend(["--model", model])

            # Spawn Claude Code CLI
            with open(log_file, "a") as f:
                process = subprocess.Popen(
                    cmd,
                    cwd=WORKING_DIR / "coding",
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    text=True
                )

            model_str = f" (model: {model})" if model else ""
            log.info(f"Spawned agent for task {task_id}: {title}{model_str}")

            return RunningAgent(
                task_id=task_id,
                title=title,
                process=process,
                started_at=datetime.now(),
                log_file=log_file
            )

        except Exception as e:
            log.error(f"Failed to spawn agent for {task_id}: {e}")
            return None

    def notify_completion(self, task_id: str, title: str, success: bool, duration: str) -> None:
        """Record task completion and optionally send Discord notification.

        Always writes to task_completions.json (which Wendy can poll for updates).
        If NOTIFY_CHANNEL is configured, also sends a Discord message.

        Args:
            task_id: The beads task ID.
            title: Human-readable task title.
            success: Whether the task completed successfully.
            duration: Human-readable duration string (e.g., "0:05:32").
        """
        status = "completed" if success else "failed"
        timestamp = datetime.now().isoformat()

        # Always write to completions file (Wendy can check this)
        completions_file = WORKING_DIR / "task_completions.json"
        try:
            completions = []
            if completions_file.exists():
                try:
                    completions = json.loads(completions_file.read_text())
                except (OSError, json.JSONDecodeError):
                    completions = []

            # Add new completion (keep last 50)
            completions.append({
                "task_id": task_id,
                "title": title,
                "status": status,
                "duration": duration,
                "timestamp": timestamp,
                "notified": False
            })
            completions = completions[-50:]

            completions_file.write_text(json.dumps(completions, indent=2))
            log.info(f"Recorded completion for {task_id}")

        except Exception as e:
            log.error(f"Failed to write completion record: {e}")

        # Send Discord notification if channel configured
        if not NOTIFY_CHANNEL:
            log.debug("No notify channel configured, skipping Discord notification")
            return

        message = f"Task `{task_id}` {status}: **{title}**\nDuration: {duration}\n"
        if success:
            message += "Awaiting review and deployment."
        else:
            message += "Check logs for errors. Use `bd reopen` to retry."

        if self._send_discord_message(NOTIFY_CHANNEL, message):
            log.info(f"Sent Discord notification for {task_id}")
            # Mark as notified
            try:
                completions = json.loads(completions_file.read_text())
                for c in completions:
                    if c["task_id"] == task_id:
                        c["notified"] = True
                completions_file.write_text(json.dumps(completions, indent=2))
            except Exception:
                pass

    def check_agents(self) -> None:
        """Poll running agents and handle completions, timeouts, and cleanup.

        For each active agent:
        - If timed out: terminate, mark failed, notify, remove from active
        - If finished: mark complete, write log footer, notify, remove from active

        Called periodically from the main loop.
        """
        finished: list[str] = []

        for task_id, agent in self.active_agents.items():
            retcode = agent.process.poll()
            duration = datetime.now() - agent.started_at

            # Check for timeout
            if retcode is None and duration.total_seconds() > AGENT_TIMEOUT:
                log.warning(f"Agent for task {task_id} timed out after {duration}")
                self._terminate_agent(agent, f"TIMEOUT: Agent killed after {duration}")
                self.complete_task(task_id, success=False)
                self.notify_completion(task_id, agent.title, False, f"{duration} (TIMEOUT)")
                finished.append(task_id)
                continue

            if retcode is not None:
                # Agent finished - treat as success unless timeout/cancel
                # Claude CLI exit codes aren't reliable indicators of task success
                # The agent completing without timeout/crash means it did its work
                success = True

                log.info(
                    f"Agent completed task {task_id} "
                    f"(exit code {retcode}) "
                    f"after {duration}"
                )

                # Mark task as complete
                self.complete_task(task_id, success=success)

                # Append completion info to log
                with open(agent.log_file, "a") as f:
                    f.write("\n" + "=" * 60 + "\n")
                    f.write(f"Completed: {datetime.now().isoformat()}\n")
                    f.write(f"Duration: {duration}\n")
                    f.write(f"Exit code: {retcode}\n")

                # Notify Wendy
                self.notify_completion(task_id, agent.title, success, str(duration))

                finished.append(task_id)

        for task_id in finished:
            del self.active_agents[task_id]

    def cleanup_old_logs(self) -> None:
        """Remove old agent log files, keeping only the most recent MAX_LOG_FILES.

        Sorts log files by modification time and deletes the oldest ones
        if the total count exceeds MAX_LOG_FILES.
        """
        try:
            log_files = sorted(LOG_DIR.glob("agent_*.log"), key=lambda f: f.stat().st_mtime)
            if len(log_files) > MAX_LOG_FILES:
                for old_log in log_files[:-MAX_LOG_FILES]:
                    old_log.unlink()
                    log.debug(f"Removed old log: {old_log.name}")
        except Exception as e:
            log.debug(f"Log cleanup error: {e}")

    def format_reset_time_pacific(self, iso_timestamp: str) -> str:
        """Convert ISO timestamp to Pacific time formatted string.

        Args:
            iso_timestamp: ISO 8601 timestamp (e.g., "2024-01-15T08:00:00Z").

        Returns:
            Human-readable Pacific time string (e.g., "Mon Jan 15, 12:00AM PT"),
            or the original timestamp if parsing fails.
        """
        if not iso_timestamp:
            return ""
        try:
            # Parse ISO timestamp
            dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
            # Convert to Pacific (UTC-8, or UTC-7 during DST)
            # Using fixed UTC-8 for simplicity (PST)
            pacific = dt.astimezone(timezone(timedelta(hours=-8)))
            return pacific.strftime("%a %b %d, %I:%M%p PT")
        except Exception as e:
            log.debug(f"Failed to parse reset time: {e}")
            return iso_timestamp

    def get_usage_state(self) -> dict:
        """Load usage notification state from disk.

        Tracks the last notified threshold percentages to avoid sending
        duplicate notifications for the same usage level.

        Returns:
            Dict with 'last_notified_week_all' and 'last_notified_week_sonnet' keys.
        """
        if USAGE_STATE_FILE.exists():
            try:
                return json.loads(USAGE_STATE_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        return {"last_notified_week_all": 0, "last_notified_week_sonnet": 0}

    def save_usage_state(self, state: dict) -> None:
        """Persist usage notification state to disk.

        Args:
            state: Dict with 'last_notified_week_all' and 'last_notified_week_sonnet'.
        """
        try:
            USAGE_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            log.error(f"Failed to save usage state: {e}")

    def check_usage(self) -> None:
        """Check Claude Code usage and send notifications if thresholds crossed.

        Runs the usage script periodically (controlled by USAGE_POLL_INTERVAL)
        or immediately if USAGE_FORCE_CHECK_FILE exists. Saves latest usage
        data to USAGE_DATA_FILE for the dashboard.

        Sends Discord notifications when usage crosses 10% thresholds
        (10%, 20%, 30%, etc.) to avoid spamming with every small change.
        """
        now = time.time()

        # Check for force check request
        force_check = USAGE_FORCE_CHECK_FILE.exists()
        if force_check:
            try:
                USAGE_FORCE_CHECK_FILE.unlink()
                log.info("Force usage check requested")
            except Exception:
                pass

        # Only check every USAGE_POLL_INTERVAL seconds (unless forced)
        if not force_check and now - self.last_usage_check < USAGE_POLL_INTERVAL:
            return

        self.last_usage_check = now

        # Skip if script doesn't exist
        if not USAGE_SCRIPT_PATH.exists():
            log.debug("Usage script not found, skipping usage check")
            return

        log.info("Checking Claude usage...")

        try:
            result = subprocess.run(
                ["bash", str(USAGE_SCRIPT_PATH)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=WORKING_DIR
            )

            if result.returncode != 0:
                log.warning(f"Usage script failed: {result.stderr}")
                return

            usage = json.loads(result.stdout)
            week_all = usage.get("week_all_percent", 0)
            week_sonnet = usage.get("week_sonnet_percent", 0)
            session_pct = usage.get("session_percent", 0)

            log.info(f"Usage: week_all={week_all}%, week_sonnet={week_sonnet}%")

            # Save latest usage data for proxy to read
            try:
                usage_data = {
                    "session_percent": session_pct,
                    "session_resets": usage.get("session_resets", ""),
                    "week_all_percent": week_all,
                    "week_all_resets": usage.get("week_all_resets", ""),
                    "week_sonnet_percent": week_sonnet,
                    "week_sonnet_resets": usage.get("week_sonnet_resets", ""),
                    "timestamp": usage.get("timestamp", datetime.now().isoformat()),
                    "updated_at": datetime.now().isoformat()
                }
                USAGE_DATA_FILE.write_text(json.dumps(usage_data, indent=2))
            except Exception as e:
                log.error(f"Failed to save usage data: {e}")

            # Check for threshold crossings and notify (only if channel configured)
            channel = USAGE_NOTIFY_CHANNEL or NOTIFY_CHANNEL
            if channel:
                state = self.get_usage_state()
                last_all = state.get("last_notified_week_all", 0)
                last_sonnet = state.get("last_notified_week_sonnet", 0)

                # Calculate 10% thresholds
                current_all_threshold = (week_all // 10) * 10
                current_sonnet_threshold = (week_sonnet // 10) * 10

                messages = []

                # Get reset times formatted for Pacific
                week_all_resets = self.format_reset_time_pacific(usage.get("week_all_resets", ""))
                week_sonnet_resets = self.format_reset_time_pacific(usage.get("week_sonnet_resets", ""))

                # Check if we crossed a new 10% threshold for all models
                if current_all_threshold > last_all and current_all_threshold > 0:
                    reset_str = f" (resets {week_all_resets})" if week_all_resets else ""
                    messages.append(f"Weekly usage (all models): {week_all}%{reset_str}")
                    state["last_notified_week_all"] = current_all_threshold

                # Check if we crossed a new 10% threshold for Sonnet
                if current_sonnet_threshold > last_sonnet and current_sonnet_threshold > 0:
                    reset_str = f" (resets {week_sonnet_resets})" if week_sonnet_resets else ""
                    messages.append(f"Weekly usage (Sonnet): {week_sonnet}%{reset_str}")
                    state["last_notified_week_sonnet"] = current_sonnet_threshold

                # Send notification if thresholds crossed
                if messages:
                    message = "Claude Code Usage Alert:\n" + "\n".join(messages)
                    self.send_usage_notification(channel, message)
                    self.save_usage_state(state)

        except subprocess.TimeoutExpired:
            log.warning("Usage check timed out")
        except json.JSONDecodeError as e:
            log.warning(f"Failed to parse usage output: {e}")
        except Exception as e:
            log.error(f"Usage check error: {e}")

    def send_usage_notification(self, channel: str, message: str) -> None:
        """Send a usage alert notification to Discord.

        Args:
            channel: Discord channel ID.
            message: Alert message content.
        """
        if self._send_discord_message(channel, message):
            log.info("Sent usage notification to Discord")

    def check_cancel_requests(self) -> None:
        """Check for and process task cancellation requests.

        Reads CANCEL_FILE for task IDs that should be cancelled. For each
        active agent matching a cancel request:
        - Terminates the agent process
        - Marks the task as failed in beads
        - Sends completion notification
        - Removes from active agents

        Cleans up the cancel file after processing.
        """
        if not CANCEL_FILE.exists():
            return

        try:
            cancel_ids = json.loads(CANCEL_FILE.read_text())
            if not cancel_ids:
                return

            processed = []
            for task_id in cancel_ids:
                if task_id in self.active_agents:
                    agent = self.active_agents[task_id]
                    log.info(f"Canceling task {task_id} by request")

                    self._terminate_agent(agent, "CANCELED by user request")
                    self.complete_task(task_id, success=False)

                    duration = datetime.now() - agent.started_at
                    self.notify_completion(task_id, agent.title, False, f"{duration} (CANCELED)")

                    del self.active_agents[task_id]
                    processed.append(task_id)

            # Remove processed IDs from cancel file
            remaining = [tid for tid in cancel_ids if tid not in processed]
            if remaining:
                CANCEL_FILE.write_text(json.dumps(remaining))
            else:
                CANCEL_FILE.unlink()

        except (OSError, json.JSONDecodeError) as e:
            log.debug(f"Cancel file error: {e}")

    def check_closed_tasks(self) -> None:
        """Check if any running tasks were closed externally and kill their agents.

        Handles the case where someone runs `bd close <task_id>` manually
        while the agent is still running. Terminates the orphaned agent
        and records it as cancelled.

        Note: If the agent process has already exited, we skip killing it
        and let the normal completion flow mark it as successful.
        """
        if not self.active_agents:
            return

        to_kill = []
        for task_id in self.active_agents:
            agent = self.active_agents[task_id]

            # Check if agent process has already finished
            if agent.process.poll() is not None:
                # Process already exited, let normal completion flow handle it
                continue

            task = self.get_task_details(task_id)
            if task and task.get("status") == "closed":
                to_kill.append(task_id)

        for task_id in to_kill:
            agent = self.active_agents[task_id]
            log.info(f"Task {task_id} was closed externally, killing agent")

            self._terminate_agent(agent, "KILLED - task closed externally")

            duration = datetime.now() - agent.started_at
            self.notify_completion(task_id, agent.title, False, f"{duration} (CANCELLED)")

            del self.active_agents[task_id]

    def init_beads(self) -> bool:
        """Initialize the beads task queue if not already initialized.

        Runs `bd init` in BEADS_DIR if the .beads directory doesn't exist.

        Returns:
            True if beads is initialized (or was already), False on error.
        """
        if (BEADS_DIR / ".beads").exists():
            return True

        log.info("Initializing beads...")
        try:
            result = subprocess.run(
                ["bd", "init"],
                capture_output=True,
                text=True,
                cwd=BEADS_DIR,
                timeout=30
            )
            if result.returncode == 0:
                log.info("Beads initialized successfully")
                return True
            else:
                log.error(f"Failed to init beads: {result.stderr}")
                return False
        except Exception as e:
            log.error(f"Failed to init beads: {e}")
            return False

    def run(self) -> None:
        """Run the main orchestrator loop.

        This is the entry point for the orchestrator. It:
        1. Initializes beads if needed
        2. Enters an infinite loop that:
           - Checks for cancel requests
           - Checks for externally closed tasks
           - Polls running agents for completion/timeout
           - Cleans up old log files
           - Checks Claude usage (hourly)
           - Fetches ready tasks and spawns agents up to concurrency limit
        3. Sleeps for POLL_INTERVAL between iterations

        This method runs forever until the process is killed.
        """
        log.info(f"Orchestrator starting with concurrency={self.concurrency}")
        log.info(f"Working directory: {WORKING_DIR}")
        log.info(f"Poll interval: {POLL_INTERVAL}s")

        # Auto-init beads if needed
        if not self.init_beads():
            log.error("Could not initialize beads, exiting")
            return

        while True:
            try:
                # Check for cancel requests
                self.check_cancel_requests()

                # Check if any running tasks were closed via 'bd close'
                self.check_closed_tasks()

                # Check on running agents (and handle timeouts)
                self.check_agents()

                # Periodic log cleanup
                self.cleanup_old_logs()

                # Check Claude usage (hourly)
                self.check_usage()

                # See if we can start more agents
                available_slots = self.concurrency - len(self.active_agents)

                if available_slots > 0:
                    ready_tasks = self.get_ready_tasks()

                    for task in ready_tasks:
                        task_id = task.get("id")

                        # Skip if already working on this task
                        if task_id in self.active_agents:
                            continue

                        # Claim and spawn
                        if self.claim_task(task_id):
                            agent = self.spawn_agent(task)
                            if agent:
                                self.active_agents[task_id] = agent
                                available_slots -= 1

                                if available_slots <= 0:
                                    break

                # Status update
                if self.active_agents:
                    log.debug(f"Active agents: {list(self.active_agents.keys())}")

            except Exception as e:
                log.exception(f"Error in orchestrator loop: {e}")

            time.sleep(POLL_INTERVAL)


def main() -> None:
    """Entry point for the orchestrator process.

    Creates an Orchestrator instance and starts the main loop.
    Intended to be run as: python -m orchestrator.main
    """
    orchestrator = Orchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
