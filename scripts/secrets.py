#!/usr/bin/env python3
"""Wendy's secrets manager - CLI for managing runtime secrets.

Usage:
    python3 secrets.py get <key>           # Get a secret value
    python3 secrets.py set <key> <value>   # Set a secret value
    python3 secrets.py delete <key>        # Delete a secret
    python3 secrets.py list                # List all secret keys (not values)
    python3 secrets.py path                # Print the secrets file path

Secrets are stored in /data/wendy/secrets/runtime.json
"""

import json
import sys
from pathlib import Path

SECRETS_FILE = Path("/data/wendy/secrets/runtime.json")


def load_secrets() -> dict:
    """Load secrets from file, return empty dict if not exists."""
    if not SECRETS_FILE.exists():
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRETS_FILE.write_text("{}")
        SECRETS_FILE.chmod(0o600)
        return {}
    try:
        return json.loads(SECRETS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def save_secrets(secrets: dict) -> None:
    """Save secrets to file."""
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.write_text(json.dumps(secrets, indent=2))
    SECRETS_FILE.chmod(0o600)


def cmd_get(key: str) -> None:
    """Get a secret value."""
    secrets = load_secrets()
    if key in secrets:
        print(secrets[key])
    else:
        print(f"Error: Secret '{key}' not found", file=sys.stderr)
        sys.exit(1)


def cmd_set(key: str, value: str) -> None:
    """Set a secret value."""
    secrets = load_secrets()
    secrets[key] = value
    save_secrets(secrets)
    print(f"Stored secret '{key}'")


def cmd_delete(key: str) -> None:
    """Delete a secret."""
    secrets = load_secrets()
    if key in secrets:
        del secrets[key]
        save_secrets(secrets)
        print(f"Deleted secret '{key}'")
    else:
        print(f"Error: Secret '{key}' not found", file=sys.stderr)
        sys.exit(1)


def cmd_list() -> None:
    """List all secret keys (not values for security)."""
    secrets = load_secrets()
    if secrets:
        for key in sorted(secrets.keys()):
            value = secrets[key]
            if isinstance(value, str):
                # Show key with masked value hint
                print(f"  {key}: {'*' * min(len(value), 8)}... ({len(value)} chars)")
            else:
                print(f"  {key}: <{type(value).__name__}>")
    else:
        print("No secrets stored.")


def cmd_path() -> None:
    """Print the secrets file path."""
    print(SECRETS_FILE)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "get" and len(sys.argv) == 3:
        cmd_get(sys.argv[2])
    elif cmd == "set" and len(sys.argv) >= 4:
        # Join remaining args in case value has spaces
        value = " ".join(sys.argv[3:])
        cmd_set(sys.argv[2], value)
    elif cmd == "delete" and len(sys.argv) == 3:
        cmd_delete(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    elif cmd == "path":
        cmd_path()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
