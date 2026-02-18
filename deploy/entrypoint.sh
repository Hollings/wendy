#!/bin/bash
# Wendy v2 entrypoint - setup hooks then run main command

# Setup Claude sync hooks (if config exists)
if [ -f /app/config/claude-sync/setup-hooks.sh ]; then
    bash /app/config/claude-sync/setup-hooks.sh
fi

# Allow git operations on bind-mounted repos
git config --global --add safe.directory /srv/wendy-bot-dev
git config --global --add safe.directory /app

# Configure Claude CLI for OAuth token auth (no interactive login needed)
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo '{"hasCompletedOnboarding": true}' > /root/.claude.json
fi

# Setup git credentials if GITHUB_PAT is set (dev mode)
if [ -n "${GITHUB_PAT:-}" ]; then
    git config --global credential.helper store
    echo "https://monsterwendy:${GITHUB_PAT}@github.com" > /root/.git-credentials
    chmod 600 /root/.git-credentials
    git config --global user.name "monsterwendy"
    git config --global user.email "wendy@wendy.monster"
fi

exec "$@"
