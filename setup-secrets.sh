#!/bin/bash
# setup-secrets.sh - Initialize Wendy's secrets on Orange Pi
# Run once after first deployment: ssh ubuntu@100.120.250.100 "cd /srv/wendy-bot && ./setup-secrets.sh"

set -euo pipefail

SECRETS_DIR="/srv/secrets/wendy"
RUNTIME_SECRETS_DIR="/var/lib/docker/volumes/wendy_data/_data/secrets"

echo "Setting up Wendy's secrets..."

# Create static secrets directory
sudo mkdir -p "$SECRETS_DIR"
sudo chown "$(whoami):$(whoami)" "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# Create runtime secrets directory (in Docker volume)
sudo mkdir -p "$RUNTIME_SECRETS_DIR"
sudo chown 1000:1000 "$RUNTIME_SECRETS_DIR"
chmod 700 "$RUNTIME_SECRETS_DIR"

# Generate random tokens
generate_token() {
    python3 -c "import secrets; print(secrets.token_hex(32))"
}

# Create bot.env if not exists
if [[ ! -f "$SECRETS_DIR/bot.env" ]]; then
    DEPLOY_TOKEN=$(generate_token)
    GAMES_TOKEN=$(generate_token)
    cat > "$SECRETS_DIR/bot.env" << EOF
# Discord bot token (from discord.com/developers)
DISCORD_TOKEN=YOUR_DISCORD_TOKEN_HERE

# Channel configuration (JSON array)
WENDY_CHANNEL_CONFIG='[{"id":"YOUR_CHANNEL_ID","name":"default","folder":"wendys_folder","mode":"full"}]'

# Deploy tokens (auto-generated)
WENDY_DEPLOY_TOKEN=$DEPLOY_TOKEN
WENDY_GAMES_TOKEN=$GAMES_TOKEN

# Service URLs
WENDY_SITES_URL=http://127.0.0.1:8910
WENDY_GAMES_URL=http://127.0.0.1:8920
EOF
    echo "Created $SECRETS_DIR/bot.env"
    echo "  >>> EDIT THIS: Add DISCORD_TOKEN and channel IDs <<<"
else
    echo "bot.env already exists, skipping"
fi

# Create sites.env if not exists
if [[ ! -f "$SECRETS_DIR/sites.env" ]]; then
    # Read DEPLOY_TOKEN from bot.env
    DEPLOY_TOKEN=$(grep WENDY_DEPLOY_TOKEN "$SECRETS_DIR/bot.env" | cut -d= -f2)
    BRAIN_SECRET=$(generate_token)
    cat > "$SECRETS_DIR/sites.env" << EOF
# Must match WENDY_DEPLOY_TOKEN in bot.env
DEPLOY_TOKEN=$DEPLOY_TOKEN

BASE_URL=https://wendy.monster

# Brain dashboard access
BRAIN_ACCESS_CODE=wendyiscool
BRAIN_SECRET=$BRAIN_SECRET
EOF
    echo "Created $SECRETS_DIR/sites.env"
else
    echo "sites.env already exists, skipping"
fi

# Create games.env if not exists
if [[ ! -f "$SECRETS_DIR/games.env" ]]; then
    GAMES_TOKEN=$(grep WENDY_GAMES_TOKEN "$SECRETS_DIR/bot.env" | cut -d= -f2)
    cat > "$SECRETS_DIR/games.env" << EOF
# Must match WENDY_GAMES_TOKEN in bot.env
DEPLOY_TOKEN=$GAMES_TOKEN
EOF
    echo "Created $SECRETS_DIR/games.env"
else
    echo "games.env already exists, skipping"
fi

# Create empty runtime.json for Wendy's own secrets
RUNTIME_JSON="$RUNTIME_SECRETS_DIR/runtime.json"
if [[ ! -f "$RUNTIME_JSON" ]]; then
    echo '{}' > "$RUNTIME_JSON"
    chmod 600 "$RUNTIME_JSON"
    echo "Created $RUNTIME_JSON (empty - Wendy will populate this)"
else
    echo "runtime.json already exists, skipping"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Static secrets (DO NOT SHARE):"
echo "  $SECRETS_DIR/bot.env    <- EDIT THIS: Add DISCORD_TOKEN and channel IDs"
echo "  $SECRETS_DIR/sites.env"
echo "  $SECRETS_DIR/games.env"
echo ""
echo "Runtime secrets (Wendy manages these):"
echo "  $RUNTIME_JSON"
echo ""
echo "Next steps:"
echo "  1. Edit $SECRETS_DIR/bot.env with your DISCORD_TOKEN and channel IDs"
echo "  2. Run: cd /srv/wendy-bot/deploy && docker compose -p wendy up -d --build"
