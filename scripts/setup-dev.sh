#!/bin/bash
# One-time setup for Wendy dev staging environment
# Run from dev machine: bash scripts/setup-dev.sh

set -euo pipefail

ORANGE_PI="ubuntu@100.120.250.100"
REPO_URL="https://github.com/Hollings/wendy.git"

echo "=== Wendy Dev Staging Setup ==="
echo ""

# Step 1: Create Docker volumes
echo "[1/5] Creating Docker volumes..."
ssh "$ORANGE_PI" "
    docker volume create wendy_dev_data
    docker volume create wendy_dev_claude_config
    docker volume create wendy_dev_repo
    docker volume create wendy_dev_sites_data
    echo 'Volumes created.'
"

# Step 2: Clone deploy repo
echo "[2/5] Cloning deploy repo to /srv/wendy-bot-dev/..."
ssh "$ORANGE_PI" "
    if [ -d /srv/wendy-bot-dev ]; then
        echo 'Deploy clone already exists, pulling latest...'
        cd /srv/wendy-bot-dev && git pull origin main --ff-only
    else
        sudo git clone $REPO_URL /srv/wendy-bot-dev
        sudo chown -R ubuntu:ubuntu /srv/wendy-bot-dev
    fi
"

# Step 3: Clone working repo into wendy_dev_repo volume
echo "[3/5] Cloning working repo into wendy_dev_repo volume..."
ssh "$ORANGE_PI" "
    # Mount the volume and clone into it
    docker run --rm \
        -v wendy_dev_repo:/repo \
        -e REPO_URL='$REPO_URL' \
        alpine/git sh -c '
            if [ -d /repo/.git ]; then
                echo \"Repo already cloned, pulling...\"
                cd /repo && git pull origin main --ff-only || true
            else
                git clone \$REPO_URL /repo
            fi
        '
"

# Step 4: Configure git user in working clone
echo "[4/5] Configuring git user in working clone..."
ssh "$ORANGE_PI" "
    docker run --rm -v wendy_dev_repo:/repo alpine/git sh -c '
        cd /repo
        git config user.name \"monsterwendy\"
        git config user.email \"wendy@wendy.monster\"
    '
"

# Step 5: Create template env files
echo "[5/5] Creating template env files..."
ssh "$ORANGE_PI" "
    if [ ! -f /srv/secrets/wendy/dev-bot.env ]; then
        cat > /srv/secrets/wendy/dev-bot.env << 'ENVEOF'
# Dev Wendy Bot Environment
# Fill in the values below before first deploy

DISCORD_TOKEN=<dev-bot-discord-token>
WENDY_CHANNEL_CONFIG=[{\"id\":\"CHANNEL_ID\",\"name\":\"wendy-dev\",\"mode\":\"full\",\"beads_enabled\":true}]
GITHUB_PAT=<github-personal-access-token>
ENVEOF
        echo 'Created dev-bot.env template'
    else
        echo 'dev-bot.env already exists, skipping'
    fi

    if [ ! -f /srv/secrets/wendy/dev-sites.env ]; then
        cat > /srv/secrets/wendy/dev-sites.env << 'ENVEOF'
# Dev Wendy Sites Environment
SITES_DEPLOY_TOKEN=dev-sites-token
ENVEOF
        echo 'Created dev-sites.env template'
    else
        echo 'dev-sites.env already exists, skipping'
    fi
"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Create a new Discord bot at https://discord.com/developers/applications"
echo "  2. Edit /srv/secrets/wendy/dev-bot.env on Orange Pi with the bot token and channel config"
echo "  3. Edit /srv/secrets/wendy/dev-sites.env if needed"
echo "  4. Add WENDY_DEV_CHANNEL_ID to /srv/secrets/wendy/bot.env (prod bot needs this)"
echo "  5. Deploy prod bot: tools/deploy wendy-bot"
echo "  6. Run !deploy main in the dev channel to start the dev stack"
