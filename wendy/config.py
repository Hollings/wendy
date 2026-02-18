"""Configuration parsing and constants.

Leaf module -- zero internal imports from wendy.
"""
from __future__ import annotations

import json
import logging
import os
import re

_LOG = logging.getLogger(__name__)

# =============================================================================
# Model Map (single definition, used everywhere)
# =============================================================================

MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# =============================================================================
# Constants
# =============================================================================

MAX_STREAM_LOG_LINES: int = 5000
PROXY_PORT: str = os.getenv("WENDY_PROXY_PORT", "8945")
CLAUDE_CLI_TIMEOUT: int = int(os.getenv("CLAUDE_CLI_TIMEOUT", "300"))
JOURNAL_NUDGE_INTERVAL: int = int(os.getenv("JOURNAL_NUDGE_INTERVAL", "10"))
DISCORD_MAX_MESSAGE_LENGTH: int = 2000
WENDY_BOT_ID: int = int(os.getenv("WENDY_BOT_USER_ID", "0"))
WENDY_BOT_NAME: str = os.getenv("WENDY_BOT_NAME", "Wendy")
WENDY_WEB_URL: str = os.getenv("WENDY_WEB_URL", "wendy.monster")
SYNTHETIC_ID_THRESHOLD: int = 9_000_000_000_000_000_000
MAX_MESSAGE_LIMIT: int = 200
DEV_MODE: bool = os.getenv("WENDY_DEV_MODE", "") == "1"
MESSAGE_LOGGER_GUILDS: set[int] = set()
_raw_guilds = os.getenv("MESSAGE_LOGGER_GUILDS", "")
for _part in _raw_guilds.split(","):
    _part = _part.strip()
    if _part:
        try:
            MESSAGE_LOGGER_GUILDS.add(int(_part))
        except ValueError:
            pass

SENSITIVE_ENV_VARS: set[str] = {
    "DISCORD_TOKEN",
    "WEBHOOK_URL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "REPLICATE_API_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "WENDY_DEPLOY_TOKEN",
    "WENDY_GAMES_TOKEN",
    "GEMINI_API_KEY",
    "CLAUDE_SYNC_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GITHUB_PAT",
}

# =============================================================================
# Channel Config Parsing
# =============================================================================

CHANNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_name(name: str) -> bool:
    return bool(name and CHANNEL_NAME_PATTERN.match(name))


def parse_channel_configs() -> dict[int, dict]:
    """Parse WENDY_CHANNEL_CONFIG env var into a dict of channel_id -> config.

    Returns raw dicts (not ChannelConfig dataclasses) for compatibility with
    the CLI and other modules that pass config dicts around.
    """
    configs: dict[int, dict] = {}
    config_json = os.getenv("WENDY_CHANNEL_CONFIG", "")
    if not config_json:
        return configs

    try:
        raw_configs = json.loads(config_json)
    except (json.JSONDecodeError, ValueError) as e:
        _LOG.error("Failed to parse WENDY_CHANNEL_CONFIG: %s", e)
        return configs

    for cfg in raw_configs:
        if "id" not in cfg or "name" not in cfg:
            _LOG.error("Channel config missing required fields: %s", cfg)
            continue

        name = cfg["name"]
        if not _validate_name(name):
            _LOG.error("Invalid channel name '%s'", name)
            continue

        folder = cfg.get("folder", name)
        if not _validate_name(folder):
            folder = name

        channel_id = int(cfg["id"])
        configs[channel_id] = {
            "id": str(cfg["id"]),
            "name": name,
            "mode": cfg.get("mode", "chat"),
            "model": cfg.get("model"),
            "beads_enabled": cfg.get("beads_enabled", False),
            "_folder": folder,
        }

    _LOG.info("Loaded %d channel configs", len(configs))
    return configs


def resolve_model(model_shorthand: str | None) -> str:
    """Resolve a model shorthand to a full model ID."""
    if not model_shorthand:
        return MODEL_MAP["sonnet"]
    return MODEL_MAP.get(model_shorthand, model_shorthand)
