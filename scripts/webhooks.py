#!/usr/bin/env python3
"""Wendy's webhook manager - CLI for managing webhook tokens.

Usage:
    python3 webhooks.py list                           # List all webhooks
    python3 webhooks.py get <channel_name>             # Get webhook URL
    python3 webhooks.py create <channel_name> <id>     # Create webhook for channel
    python3 webhooks.py regenerate <channel_name>      # Regenerate token
    python3 webhooks.py delete <channel_name>          # Delete webhook

Webhooks are stored in /data/wendy/secrets/webhooks.json
"""

import json
import sys
import uuid
from pathlib import Path

WEBHOOKS_FILE = Path("/data/wendy/secrets/webhooks.json")
BASE_URL = "https://wendy.monster/webhook"


def load_webhooks() -> dict:
    """Load webhooks from file, return empty dict if not exists."""
    if not WEBHOOKS_FILE.exists():
        WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEBHOOKS_FILE.write_text("{}")
        WEBHOOKS_FILE.chmod(0o600)
        return {}
    try:
        return json.loads(WEBHOOKS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_webhooks(webhooks: dict) -> None:
    """Save webhooks to file."""
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBHOOKS_FILE.write_text(json.dumps(webhooks, indent=2))
    WEBHOOKS_FILE.chmod(0o600)


def generate_token() -> str:
    """Generate a secure random token."""
    return str(uuid.uuid4())


def cmd_list() -> None:
    """List all webhooks."""
    webhooks = load_webhooks()
    if not webhooks:
        print("No webhooks configured.")
        return

    print("Configured webhooks:")
    for name, data in sorted(webhooks.items()):
        channel_id = data.get("channel_id", "unknown")
        token = data.get("token", "")
        # Show truncated token for security
        token_preview = f"{token[:8]}..." if len(token) > 8 else token
        print(f"  {name}: channel={channel_id}, token={token_preview}")


def cmd_get(channel_name: str) -> None:
    """Get webhook URL for a channel."""
    webhooks = load_webhooks()
    if channel_name not in webhooks:
        print(f"Error: No webhook for channel '{channel_name}'", file=sys.stderr)
        print(f"Use 'webhooks.py create {channel_name} <channel_id>' to create one", file=sys.stderr)
        sys.exit(1)

    token = webhooks[channel_name].get("token", "")
    url = f"{BASE_URL}/{token}"
    print(url)


def cmd_create(channel_name: str, channel_id: str) -> None:
    """Create a new webhook for a channel."""
    webhooks = load_webhooks()

    if channel_name in webhooks:
        print(f"Error: Webhook for '{channel_name}' already exists", file=sys.stderr)
        print("Use 'webhooks.py regenerate' to get a new token", file=sys.stderr)
        sys.exit(1)

    # Validate channel_id is numeric
    try:
        int(channel_id)
    except ValueError:
        print(f"Error: Channel ID must be numeric, got '{channel_id}'", file=sys.stderr)
        sys.exit(1)

    token = generate_token()
    webhooks[channel_name] = {
        "token": token,
        "channel_id": channel_id,
    }
    save_webhooks(webhooks)

    url = f"{BASE_URL}/{token}"
    print(f"Created webhook for '{channel_name}'")
    print(f"URL: {url}")


def cmd_regenerate(channel_name: str) -> None:
    """Regenerate token for an existing webhook."""
    webhooks = load_webhooks()

    if channel_name not in webhooks:
        print(f"Error: No webhook for channel '{channel_name}'", file=sys.stderr)
        sys.exit(1)

    old_token = webhooks[channel_name].get("token", "")[:8]
    new_token = generate_token()
    webhooks[channel_name]["token"] = new_token
    save_webhooks(webhooks)

    url = f"{BASE_URL}/{new_token}"
    print(f"Regenerated token for '{channel_name}'")
    print(f"Old token {old_token}... is now invalid")
    print(f"New URL: {url}")


def cmd_delete(channel_name: str) -> None:
    """Delete a webhook."""
    webhooks = load_webhooks()

    if channel_name not in webhooks:
        print(f"Error: No webhook for channel '{channel_name}'", file=sys.stderr)
        sys.exit(1)

    del webhooks[channel_name]
    save_webhooks(webhooks)
    print(f"Deleted webhook for '{channel_name}'")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        cmd_list()
    elif cmd == "get" and len(sys.argv) == 3:
        cmd_get(sys.argv[2])
    elif cmd == "create" and len(sys.argv) == 4:
        cmd_create(sys.argv[2], sys.argv[3])
    elif cmd == "regenerate" and len(sys.argv) == 3:
        cmd_regenerate(sys.argv[2])
    elif cmd == "delete" and len(sys.argv) == 3:
        cmd_delete(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
