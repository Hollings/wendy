"""Dev deployment commands for Wendy Bot.

This cog provides Discord commands to deploy and revert the dev staging environment.
Only loads if WENDY_DEV_CHANNEL_ID is set, and only responds in that channel.

Commands:
    !deploy [branch] - Deploy a git branch to dev.wendy.monster (default: main)
    !revert [ref] - Revert to a specific branch or commit (alias for deploy)

Architecture:
    Dev channel -> Discord command -> Git checkout -> Docker rebuild -> dev.wendy.monster
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from discord.ext import commands

_LOG = logging.getLogger(__name__)

# Branch/ref validation regex - allow alphanumeric, dots, underscores, slashes, hyphens
VALID_REF_PATTERN = re.compile(r"^[a-zA-Z0-9_./-]+$")

# Deployment paths
DEV_REPO_PATH = "/srv/wendy-bot-dev"
DEV_COMPOSE_FILE = "/srv/wendy-bot-dev/deploy/docker-compose.dev.yml"
DEV_PROJECT_NAME = "wendy-dev"


class DeployCog(commands.Cog):
    """Discord cog for deploying Wendy Bot to the dev staging environment.

    This cog provides commands to trigger git checkout + docker rebuild cycles
    for rapid iteration on dev.wendy.monster. Only loads if WENDY_DEV_CHANNEL_ID
    is set in the environment, and only responds in that specific channel.

    Attributes:
        bot: The Discord bot instance.
        dev_channel_id: The channel ID where deploy commands are allowed.
        deploy_lock: Asyncio lock to prevent concurrent deploys.
    """

    def __init__(self, bot: commands.Bot, dev_channel_id: int) -> None:
        """Initialize the deploy cog.

        Args:
            bot: The Discord bot instance.
            dev_channel_id: The Discord channel ID where commands are allowed.
        """
        self.bot = bot
        self.dev_channel_id = dev_channel_id
        self.deploy_lock = asyncio.Lock()
        _LOG.info("DeployCog initialized for channel ID %s", dev_channel_id)

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Global check: only allow commands in the dev channel.

        Args:
            ctx: The command context.

        Returns:
            True if the command is in the dev channel, False otherwise.
        """
        return ctx.channel.id == self.dev_channel_id

    def _validate_ref(self, ref: str) -> bool:
        """Validate a git branch or commit ref name.

        Args:
            ref: The branch or commit ref to validate.

        Returns:
            True if the ref is valid, False otherwise.
        """
        return bool(VALID_REF_PATTERN.match(ref))

    async def _run_shell_command(
        self, command: str, cwd: str | None = None
    ) -> tuple[int, str, str]:
        """Run a shell command and capture output.

        Args:
            command: The shell command to execute.
            cwd: Optional working directory for the command.

        Returns:
            Tuple of (return_code, stdout, stderr).
        """
        _LOG.info("Running shell command: %s (cwd: %s)", command, cwd or "default")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        _LOG.info(
            "Command exited with code %s (stdout: %s bytes, stderr: %s bytes)",
            proc.returncode,
            len(stdout),
            len(stderr),
        )
        return proc.returncode or 0, stdout, stderr

    async def _run_with_progress(
        self, command: str, status_msg, prefix: str, cwd: str | None = None
    ) -> tuple[int, str]:
        """Run a shell command with live progress updates to a Discord message.

        Merges stdout+stderr, reads line by line, and edits the status message
        every 10 seconds with the latest output line.

        Args:
            command: The shell command to execute.
            status_msg: The Discord message to edit with progress.
            prefix: Status prefix shown before the log line.
            cwd: Optional working directory for the command.

        Returns:
            Tuple of (return_code, full_output).
        """
        _LOG.info("Running with progress: %s (cwd: %s)", command, cwd or "default")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        last_update = 0.0
        last_line = ""
        all_output = []

        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            if line:
                all_output.append(line)
                last_line = line
                now = asyncio.get_event_loop().time()
                if now - last_update >= 10:
                    last_update = now
                    display_line = last_line[:120]
                    try:
                        await status_msg.edit(
                            content=f"{prefix}\n```{display_line}```"
                        )
                    except Exception:
                        pass

        await proc.wait()
        _LOG.info("Progress command exited with code %s", proc.returncode)
        return proc.returncode or 0, "\n".join(all_output)

    async def _perform_deploy(
        self, ctx: commands.Context, ref: str
    ) -> None:
        """Execute the deployment workflow.

        Args:
            ctx: The command context.
            ref: The git branch or commit ref to deploy.
        """
        # Validate ref
        if not self._validate_ref(ref):
            await ctx.send(f"Invalid ref: `{ref}` (must be alphanumeric with .-_/)")
            return

        # Acquire lock
        if self.deploy_lock.locked():
            await ctx.send("Deploy already in progress, please wait.")
            return

        async with self.deploy_lock:
            try:
                # Step 1: Fetch
                status_msg = await ctx.send("Fetching latest changes from origin...")
                code, stdout, stderr = await self._run_shell_command(
                    "git fetch origin", cwd=DEV_REPO_PATH
                )
                if code != 0:
                    await status_msg.edit(
                        content=f"Failed to fetch: ```{stderr or stdout}```"
                    )
                    return

                # Step 2: Checkout and pull
                await status_msg.edit(
                    content=f"Checking out `{ref}` and pulling latest..."
                )
                code, stdout, stderr = await self._run_shell_command(
                    f"git checkout -B {ref} origin/{ref}",
                    cwd=DEV_REPO_PATH,
                )
                if code != 0:
                    await status_msg.edit(
                        content=f"Failed to checkout/pull: ```{stderr or stdout}```"
                    )
                    return

                # Step 3: Stop existing containers
                await status_msg.edit(content="Stopping existing containers...")
                code, stdout, stderr = await self._run_shell_command(
                    f"docker compose -f {DEV_COMPOSE_FILE} -p {DEV_PROJECT_NAME} down --remove-orphans"
                )
                if code != 0:
                    _LOG.warning("Docker down had non-zero exit: %s", stderr or stdout)

                # Step 4: Build and start new containers (with live progress)
                await status_msg.edit(content="Building and starting containers...")
                code, output = await self._run_with_progress(
                    f"docker compose -f {DEV_COMPOSE_FILE} -p {DEV_PROJECT_NAME} up -d --build",
                    status_msg,
                    "Building and starting containers...",
                )
                if code != 0:
                    # Show last 500 chars of output on failure
                    tail = output[-500:] if len(output) > 500 else output
                    await status_msg.edit(
                        content=f"Failed to build/start: ```{tail}```"
                    )
                    return

                # Step 5: Get commit SHA
                code, sha, stderr = await self._run_shell_command(
                    "git rev-parse --short HEAD", cwd=DEV_REPO_PATH
                )
                if code != 0:
                    sha = "unknown"

                # Step 6: Success
                await status_msg.edit(
                    content=f"Deployed `{ref}` ({sha}) to dev.wendy.monster"
                )
                _LOG.info("Deploy successful: ref=%s, sha=%s", ref, sha)

            except Exception as e:
                _LOG.error("Deploy failed with exception: %s", e, exc_info=True)
                await ctx.send(f"Deploy failed: {e}")

    @commands.command(name="deploy")
    async def deploy(self, ctx: commands.Context, branch: str = "main") -> None:
        """Deploy a git branch to the dev staging environment.

        Args:
            ctx: The command context.
            branch: The git branch to deploy (default: main).
        """
        _LOG.info("Deploy command invoked: branch=%s, user=%s", branch, ctx.author)
        await self._perform_deploy(ctx, branch)

    @commands.command(name="revert")
    async def revert(self, ctx: commands.Context, ref: str = "main") -> None:
        """Revert to a specific branch or commit ref.

        This is an alias for the deploy command, provided for semantic clarity.

        Args:
            ctx: The command context.
            ref: The git branch or commit ref to revert to (default: main).
        """
        _LOG.info("Revert command invoked: ref=%s, user=%s", ref, ctx.author)
        await self._perform_deploy(ctx, ref)


async def setup(bot: commands.Bot) -> None:
    """Load the deploy cog if WENDY_DEV_CHANNEL_ID is set.

    Args:
        bot: The Discord bot instance.
    """
    dev_channel_id_str = os.getenv("WENDY_DEV_CHANNEL_ID")
    if not dev_channel_id_str:
        _LOG.info("WENDY_DEV_CHANNEL_ID not set, skipping DeployCog")
        return

    try:
        dev_channel_id = int(dev_channel_id_str)
    except ValueError:
        _LOG.error("WENDY_DEV_CHANNEL_ID is not a valid integer: %s", dev_channel_id_str)
        return

    await bot.add_cog(DeployCog(bot, dev_channel_id))
    _LOG.info("DeployCog loaded for channel %s", dev_channel_id)
