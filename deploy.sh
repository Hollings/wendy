#!/usr/bin/env bash
# services/wendy-bot/deploy.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="wendy-bot"
REMOTE_HOST="${DEPLOY_HOST:-ubuntu@100.120.250.100}"
REMOTE_BASE="/srv"

echo "[$(date +%H:%M:%S)] Deploying $SERVICE..."

# Create tarball
echo "[$(date +%H:%M:%S)] Packaging..."
tar --exclude='__pycache__' --exclude='.git' --exclude='*.pyc' --exclude='*.egg-info' \
    -czf "/tmp/${SERVICE}.tar.gz" -C "$SCRIPT_DIR" .

# Upload
echo "[$(date +%H:%M:%S)] Uploading..."
scp "/tmp/${SERVICE}.tar.gz" "${REMOTE_HOST}:/tmp/"

# Extract and restart
echo "[$(date +%H:%M:%S)] Deploying on remote..."
ssh "$REMOTE_HOST" "
    mkdir -p ${REMOTE_BASE}/${SERVICE}
    tar -xzf /tmp/${SERVICE}.tar.gz -C ${REMOTE_BASE}/${SERVICE}
    rm /tmp/${SERVICE}.tar.gz

    # Create .env from example if not exists
    if [ ! -f ${REMOTE_BASE}/${SERVICE}/deploy/.env ]; then
        cp ${REMOTE_BASE}/${SERVICE}/deploy/.env.example ${REMOTE_BASE}/${SERVICE}/deploy/.env
        echo 'Created .env from .env.example - please configure tokens'
    fi

    cd ${REMOTE_BASE}/${SERVICE}/deploy
    docker compose -p ${SERVICE} up -d --build
"

rm "/tmp/${SERVICE}.tar.gz"

echo "[$(date +%H:%M:%S)] Deployed! Testing health..."
sleep 3
ssh "$REMOTE_HOST" "curl -s http://localhost:8945/health || echo 'Proxy not ready yet'"
echo ""
echo "[$(date +%H:%M:%S)] Done!"
echo ""
echo "Next steps:"
echo "  1. SSH to Orange Pi: ssh ubuntu@100.120.250.100"
echo "  2. Check/edit .env: vi /srv/wendy-bot/deploy/.env"
echo "  3. Login to Claude CLI: docker exec -it wendy-bot claude login"
