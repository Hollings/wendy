"""
Bead Orchestrator - Spawns Claude Code agents to work on tasks from the beads queue.

Polls `bd ready` for available tasks and spawns agents up to the concurrency limit.
Each agent is given a task and works autonomously until completion.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Thread
from typing import Optional

# Configuration
CONCURRENCY = int(os.getenv("ORCHESTRATOR_CONCURRENCY", "1"))
POLL_INTERVAL = int(os.getenv("ORCHESTRATOR_POLL_INTERVAL", "30"))  # seconds
WORKING_DIR = Path(os.getenv("ORCHESTRATOR_WORKING_DIR", "/data/wendy"))
BEADS_DIR = WORKING_DIR  # Where .beads/ lives
LOG_DIR = WORKING_DIR / "orchestrator_logs"
PROXY_URL = os.getenv("ORCHESTRATOR_PROXY_URL", "http://127.0.0.1:8945")
NOTIFY_CHANNEL = os.getenv("ORCHESTRATOR_NOTIFY_CHANNEL", "")
AGENT_TIMEOUT = int(os.getenv("ORCHESTRATOR_AGENT_TIMEOUT", "1800"))  # 30 minutes
MAX_LOG_FILES = int(os.getenv("ORCHESTRATOR_MAX_LOG_FILES", "50"))
AGENT_SYSTEM_PROMPT_FILE = Path(os.getenv("AGENT_SYSTEM_PROMPT_FILE", "/app/config/agent_claude_md.txt"))
CANCEL_FILE = WORKING_DIR / "cancel_tasks.json"

# Usage monitoring configuration
USAGE_POLL_INTERVAL = int(os.getenv("ORCHESTRATOR_USAGE_POLL_INTERVAL", "3600"))  # 1 hour
USAGE_NOTIFY_CHANNEL = os.getenv("ORCHESTRATOR_USAGE_NOTIFY_CHANNEL", "")  # Channel for usage alerts
USAGE_STATE_FILE = WORKING_DIR / "usage_state.json"
USAGE_DATA_FILE = WORKING_DIR / "usage_data.json"  # Latest usage for proxy to read
USAGE_FORCE_CHECK_FILE = WORKING_DIR / "usage_force_check"  # Trigger immediate check
USAGE_SCRIPT_PATH = Path("/app/scripts/get_usage.sh")

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
    """Tracks a running Claude Code agent."""
    task_id: str
    title: str
    process: subprocess.Popen
    started_at: datetime
    log_file: Path


class Orchestrator:
    def __init__(self):
        self.active_agents: dict[str, RunningAgent] = {}
        self.concurrency = CONCURRENCY
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.last_usage_check = 0.0  # timestamp of last usage check

    def get_ready_tasks(self) -> list[dict]:
        """Get list of ready, unassigned tasks from beads, sorted by priority."""
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
        """Mark task as in-progress (atomically claim it)."""
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
        """Mark task as complete or failed."""
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

    def get_task_details(self, task_id: str) -> Optional[dict]:
        """Get full task details."""
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

    def parse_model_from_labels(self, labels: list) -> Optional[str]:
        """Extract model from labels like 'model:opus' -> 'opus'."""
        for label in labels or []:
            if label.startswith("model:"):
                return label.split(":", 1)[1]
        return None

    def spawn_agent(self, task: dict) -> Optional[RunningAgent]:
        """Spawn a Claude Code agent to work on a task."""
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
        prompt = f"""You have been assigned a task from the work queue.

Task ID: {task_id}
Title: {title}

Description:
{description}

Instructions:
1. Complete this task thoroughly
2. Work in /data/wendy/wendys_folder/ unless the task specifies otherwise
3. Test your changes locally if applicable
4. When done, summarize what you accomplished

IMPORTANT: Do NOT deploy anything. Your job is to write the code only.
Wendy will review your work and handle deployment separately.

Begin working on this task now."""

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

            # Build CLI command
            # Note: Can't use --dangerously-skip-permissions or --permission-mode bypassPermissions
            # when running as root. Use --allowedTools to whitelist required tools instead.
            # -p = print mode (non-interactive), prompt is positional arg
            # --verbose required for stream-json output
            cmd = [
                "claude",
                "-p", prompt,
                "--max-turns", "9999",
                "--allowedTools", "Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite",
                "--output-format", "stream-json",
                "--verbose"
            ]

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
                    cwd=WORKING_DIR / "wendys_folder",
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

    def notify_completion(self, task_id: str, title: str, success: bool, duration: str):
        """Record completion and optionally send Discord notification."""
        status = "completed" if success else "failed"
        timestamp = datetime.now().isoformat()

        # Always write to completions file (Wendy can check this)
        completions_file = WORKING_DIR / "task_completions.json"
        try:
            completions = []
            if completions_file.exists():
                try:
                    completions = json.loads(completions_file.read_text())
                except (json.JSONDecodeError, IOError):
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

        try:
            data = json.dumps({
                "channel_id": NOTIFY_CHANNEL,
                "content": message
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{PROXY_URL}/api/send_message",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
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
                else:
                    log.warning(f"Notification failed with status {resp.status}")

        except urllib.error.URLError as e:
            log.warning(f"Failed to send Discord notification: {e}")
        except Exception as e:
            log.error(f"Notification error: {e}")

    def check_agents(self):
        """Check status of running agents, handle timeouts, and clean up finished ones."""
        finished = []

        for task_id, agent in self.active_agents.items():
            retcode = agent.process.poll()
            duration = datetime.now() - agent.started_at

            # Check for timeout
            if retcode is None and duration.total_seconds() > AGENT_TIMEOUT:
                log.warning(f"Agent for task {task_id} timed out after {duration}")
                try:
                    agent.process.terminate()
                    agent.process.wait(timeout=5)
                except Exception:
                    agent.process.kill()

                # Mark as failed due to timeout
                self.complete_task(task_id, success=False)

                with open(agent.log_file, "a") as f:
                    f.write("\n" + "=" * 60 + "\n")
                    f.write(f"TIMEOUT: Agent killed after {duration}\n")
                    f.write(f"Completed: {datetime.now().isoformat()}\n")

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

    def cleanup_old_logs(self):
        """Remove old log files, keeping only the most recent MAX_LOG_FILES."""
        try:
            log_files = sorted(LOG_DIR.glob("agent_*.log"), key=lambda f: f.stat().st_mtime)
            if len(log_files) > MAX_LOG_FILES:
                for old_log in log_files[:-MAX_LOG_FILES]:
                    old_log.unlink()
                    log.debug(f"Removed old log: {old_log.name}")
        except Exception as e:
            log.debug(f"Log cleanup error: {e}")

    def format_reset_time_pacific(self, iso_timestamp: str) -> str:
        """Convert ISO timestamp to Pacific time formatted string."""
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
        """Load usage state (last notified thresholds)."""
        if USAGE_STATE_FILE.exists():
            try:
                return json.loads(USAGE_STATE_FILE.read_text())
            except (json.JSONDecodeError, IOError):
                pass
        return {"last_notified_week_all": 0, "last_notified_week_sonnet": 0}

    def save_usage_state(self, state: dict):
        """Save usage state."""
        try:
            USAGE_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            log.error(f"Failed to save usage state: {e}")

    def check_usage(self):
        """Check Claude usage and notify if thresholds crossed."""
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

        # Skip if no channel configured
        channel = USAGE_NOTIFY_CHANNEL or NOTIFY_CHANNEL
        if not channel:
            log.debug("No usage notify channel configured, skipping")
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

            # Check for threshold crossings
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

    def send_usage_notification(self, channel: str, message: str):
        """Send a usage notification to Discord."""
        try:
            data = json.dumps({
                "channel_id": channel,
                "content": message
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{PROXY_URL}/api/send_message",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    log.info("Sent usage notification to Discord")
                else:
                    log.warning(f"Usage notification failed with status {resp.status}")

        except Exception as e:
            log.error(f"Failed to send usage notification: {e}")

    def check_cancel_requests(self):
        """Check for and process cancel requests."""
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

                    try:
                        agent.process.terminate()
                        agent.process.wait(timeout=5)
                    except Exception:
                        agent.process.kill()

                    # Mark as failed (canceled)
                    self.complete_task(task_id, success=False)

                    with open(agent.log_file, "a") as f:
                        f.write("\n" + "=" * 60 + "\n")
                        f.write(f"CANCELED by user request\n")
                        f.write(f"Completed: {datetime.now().isoformat()}\n")

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

        except (json.JSONDecodeError, IOError) as e:
            log.debug(f"Cancel file error: {e}")

    def init_beads(self):
        """Initialize beads if not already initialized."""
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

    def run(self):
        """Main orchestrator loop."""
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


def main():
    orchestrator = Orchestrator()
    orchestrator.run()


if __name__ == "__main__":
    main()
