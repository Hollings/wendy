"""Wendy Bot - Discord bot entry point."""

import asyncio
import logging
import os
import signal
import sys

import discord
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

_LOG = logging.getLogger("wendy")


def get_bot() -> commands.Bot:
    """Create and configure the bot instance."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    bot = commands.Bot(
        command_prefix=commands.when_mentioned_or("!"),
        intents=intents,
        help_command=None,
    )

    @bot.event
    async def on_ready():
        _LOG.info("Wendy logged in as %s (ID: %s)", bot.user.name, bot.user.id)
        _LOG.info("Connected to %d guilds", len(bot.guilds))

    @bot.event
    async def on_connect():
        _LOG.info("Connected to Discord")

    @bot.event
    async def on_disconnect():
        _LOG.warning("Disconnected from Discord")

    return bot


async def load_extensions(bot: commands.Bot) -> None:
    """Load all bot extensions/cogs."""
    extensions = [
        "bot.wendy_cog",
        "bot.wendy_outbox",
    ]

    for ext in extensions:
        try:
            await bot.load_extension(ext)
            _LOG.info("Loaded extension: %s", ext)
        except Exception as e:
            _LOG.error("Failed to load extension %s: %s", ext, e)
            raise


async def main() -> None:
    """Main entry point."""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        _LOG.error("DISCORD_TOKEN environment variable not set")
        sys.exit(1)

    bot = get_bot()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def handle_signal(sig):
        _LOG.info("Received signal %s, shutting down...", sig.name)
        asyncio.create_task(bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal, sig)

    async with bot:
        await load_extensions(bot)
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
