# Webhooks

External services (GitHub, CI/CD, etc.) can POST to webhook URLs to wake Wendy with data.

## Get your webhook URL

```bash
python3 /app/scripts/webhooks.py get <channel_name>
```

URL format: `https://wendy.monster/webhook/{token}`

## Management

```bash
python3 /app/scripts/webhooks.py list
python3 /app/scripts/webhooks.py create <name> <channel_id>
python3 /app/scripts/webhooks.py regenerate <name>   # invalidates old token
python3 /app/scripts/webhooks.py delete <name>
```

## Supported sources (auto-detected from headers)

- GitHub (push, pull_request, issues, release, etc.)
- GitLab (push, merge request, etc.)
- Generic (any JSON payload)

## When events arrive

Events appear as messages from "Webhook: {source}" (e.g., "Webhook: Github"). Respond naturally - acknowledge, take action if relevant, or note it for later.
