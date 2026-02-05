#!/usr/bin/env bash
# services/wendy-bot/deploy.sh
#
# Deploy wendy-bot services to Orange Pi.
# Secrets are stored in /srv/secrets/wendy/ (not touched by deployments).
#
# Usage:
#   ./deploy.sh           # Full deploy (rebuild Docker containers)
#   ./deploy.sh --static  # Quick deploy (static files only, no Docker rebuild)
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="wendy-bot"
REMOTE_HOST="${DEPLOY_HOST:-ubuntu@100.120.250.100}"
REMOTE_BASE="/srv"

# Parse args
STATIC_ONLY=false
if [[ "${1:-}" == "--static" ]]; then
    STATIC_ONLY=true
fi

echo "[$(date +%H:%M:%S)] Deploying $SERVICE..."

# Create tarball (exclude node_modules, caches, local .env files)
echo "[$(date +%H:%M:%S)] Packaging..."
tar --exclude='node_modules' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='*.pyc' \
    --exclude='*.egg-info' \
    --exclude='deploy/.env' \
    --exclude='wendy-sites/deploy/.env' \
    --exclude='wendy-games/deploy/.env' \
    -czf "/tmp/${SERVICE}.tar.gz" -C "$SCRIPT_DIR" .

# Upload
echo "[$(date +%H:%M:%S)] Uploading..."
scp "/tmp/${SERVICE}.tar.gz" "${REMOTE_HOST}:/tmp/"

if $STATIC_ONLY; then
    # Quick deploy - just extract static files, no Docker rebuild
    echo "[$(date +%H:%M:%S)] Quick deploy (static files only)..."
    ssh "$REMOTE_HOST" "
        tar -xzf /tmp/${SERVICE}.tar.gz -C ${REMOTE_BASE}/${SERVICE}
        rm /tmp/${SERVICE}.tar.gz
        echo 'Static files updated!'
    "
else
    # Full deploy - extract and rebuild Docker (secrets in /srv/secrets/wendy/ are untouched)
    echo "[$(date +%H:%M:%S)] Full deploy (with Docker rebuild)..."
    ssh "$REMOTE_HOST" "
        # Verify secrets exist
        if [ ! -f /srv/secrets/wendy/bot.env ]; then
            echo 'ERROR: /srv/secrets/wendy/bot.env not found!'
            echo 'Run setup-secrets.sh first to initialize secrets.'
            exit 1
        fi

        # Clean and extract
        rm -rf ${REMOTE_BASE}/${SERVICE}
        mkdir -p ${REMOTE_BASE}/${SERVICE}
        tar -xzf /tmp/${SERVICE}.tar.gz -C ${REMOTE_BASE}/${SERVICE}
        rm /tmp/${SERVICE}.tar.gz

        # Copy avatar files to wendy-sites static directory
        if [ -d ${REMOTE_BASE}/${SERVICE}/wendy-avatar ]; then
            cp -r ${REMOTE_BASE}/${SERVICE}/wendy-avatar/* ${REMOTE_BASE}/${SERVICE}/wendy-sites/backend/static/avatar/
            echo 'Copied avatar files to wendy-sites static'
        fi

        # Rebuild and restart Docker containers
        cd ${REMOTE_BASE}/${SERVICE}/deploy
        docker compose down
        docker compose up -d --build

        # Ensure wendy-games is running (proxy depends on it)
        if ! docker ps -q -f name=wendy-games-manager | grep -q .; then
            echo 'wendy-games-manager not running, starting it...'
            if [ -f /srv/wendy-games/deploy/docker-compose.yml ]; then
                cd /srv/wendy-games/deploy
                docker compose up -d --build
            else
                echo 'WARNING: wendy-games not deployed at /srv/wendy-games/'
            fi
        fi

        # Ensure wendy-sites is running (proxy depends on it)
        if ! docker ps -q -f name=wendy-sites | grep -q .; then
            echo 'wendy-sites not running, starting it...'
            if [ -f /srv/wendy-bot/wendy-sites/deploy/docker-compose.yml ]; then
                cd /srv/wendy-bot/wendy-sites/deploy
                docker compose up -d --build
            else
                echo 'WARNING: wendy-sites not found'
            fi
        fi
    "
fi

rm "/tmp/${SERVICE}.tar.gz"

echo "[$(date +%H:%M:%S)] Deployed!"

if ! $STATIC_ONLY; then
    echo "[$(date +%H:%M:%S)] Checking container status..."
    sleep 3
    ssh "$REMOTE_HOST" "docker ps --filter name=wendy --format 'table {{.Names}}\t{{.Status}}' | head -10"

    echo ""
    echo "[$(date +%H:%M:%S)] Verifying service health..."
    ssh "$REMOTE_HOST" "
        # Check wendy-games health
        if curl -sf http://127.0.0.1:8920/health > /dev/null 2>&1; then
            echo 'wendy-games: OK'
        else
            echo 'wendy-games: NOT RESPONDING'
        fi
        # Check wendy-sites health
        if curl -sf http://127.0.0.1:8910/health > /dev/null 2>&1; then
            echo 'wendy-sites: OK'
        else
            echo 'wendy-sites: NOT RESPONDING'
        fi
    "
fi

echo ""
echo "[$(date +%H:%M:%S)] Done!"
