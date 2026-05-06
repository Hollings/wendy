#!/bin/bash
# Wendy v2 entrypoint - setup hooks, permissions, then run main command

# Setup Claude sync hooks (if config exists)
if [ -f /app/config/claude-sync/setup-hooks.sh ]; then
    bash /app/config/claude-sync/setup-hooks.sh
fi

# Allow git operations on bind-mounted repos
git config --global --add safe.directory /srv/wendy-v2
git config --global --add safe.directory /app

# Ensure Claude CLI onboarding is marked complete for both users.
# Without this the CLI enters interactive onboarding and exits silently.
echo '{"hasCompletedOnboarding": true}' > /root/.claude.json
if [ ! -f /home/wendy/.claude.json ]; then
    echo '{"hasCompletedOnboarding": true}' > /home/wendy/.claude.json
fi
chown wendy:wendy /home/wendy/.claude.json

# Setup git credentials if GITHUB_PAT is set (dev mode)
if [ -n "${GITHUB_PAT:-}" ]; then
    # Root user
    git config --global credential.helper store
    echo "https://monsterwendy:${GITHUB_PAT}@github.com" > /root/.git-credentials
    chmod 600 /root/.git-credentials
    git config --global user.name "monsterwendy"
    git config --global user.email "wendy@wendy.monster"

    # Wendy user (CLI subprocess)
    cp /root/.git-credentials /home/wendy/.git-credentials
    chown wendy:wendy /home/wendy/.git-credentials
    chmod 600 /home/wendy/.git-credentials
    HOME=/home/wendy git config --global credential.helper store
    HOME=/home/wendy git config --global user.name "monsterwendy"
    HOME=/home/wendy git config --global user.email "wendy@wendy.monster"
    chown wendy:wendy /home/wendy/.gitconfig
fi

# Git safe directories for wendy user
HOME=/home/wendy git config --global --add safe.directory /app
HOME=/home/wendy git config --global --add safe.directory /srv/wendy-v2
chown wendy:wendy /home/wendy/.gitconfig 2>/dev/null || true

# ---------------------------------------------------------------------------
# CLI subprocess isolation: wendy user (UID 1000)
#
# The bot process runs as root. The Claude CLI subprocess runs as the wendy
# user. Filesystem ownership controls what the CLI can modify:
#   - writable: channels, shared (DB), tmp, people fragments, CLI sessions
#   - read-only: core fragments, hooks, config (enforced by root ownership)
# ---------------------------------------------------------------------------

# Symlink so wendy's HOME-based .claude path resolves to the volume.
# /root must be traversable (711) for wendy to follow the symlink.
chmod 711 /root
ln -sfn /root/.claude /home/wendy/.claude

# Ensure base directories exist
mkdir -p /data/wendy/channels /data/wendy/shared /data/wendy/tmp
mkdir -p /data/wendy/claude_fragments/people

# Remove MCP auth cache -- first-party integrations (Gmail, Calendar) synced
# from claude.ai cannot complete OAuth in headless mode and block the CLI.
rm -f /root/.claude/mcp-needs-auth-cache.json

# Remove stale credentials file -- if someone ran `claude login` interactively,
# it writes .credentials.json which takes priority over CLAUDE_CODE_OAUTH_TOKEN.
# When that session expires the CLI fails silently. Always use the env var.
rm -f /root/.claude/.credentials.json

# Writable areas: owned by wendy
chown -R wendy:wendy /root/.claude/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/channels/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/.beads/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/shared/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/tmp/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/secrets/ 2>/dev/null || true
chown -R wendy:wendy /data/wendy/claude_fragments/people/ 2>/dev/null || true

# Read-only areas: fragments dir owned by root (except people/)
chown root:root /data/wendy/claude_fragments/ 2>/dev/null || true
chmod 755 /data/wendy/claude_fragments/ 2>/dev/null || true
find /data/wendy/claude_fragments/ -maxdepth 1 -type f -exec chown root:root {} + 2>/dev/null || true
find /data/wendy/claude_fragments/ -maxdepth 1 -type f -exec chmod 644 {} + 2>/dev/null || true
# Subdirectories other than people/ are also root-owned
find /data/wendy/claude_fragments/ -mindepth 1 -maxdepth 1 -type d ! -name people -exec chown -R root:root {} + 2>/dev/null || true

exec "$@"
