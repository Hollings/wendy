#!/usr/bin/env bash
# services/wendy-sites/deploy.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="wendy-sites"
REMOTE_HOST="${DEPLOY_HOST:-ubuntu@100.120.250.100}"
REMOTE_BASE="/srv"

echo "[$(date +%H:%M:%S)] Deploying $SERVICE..."

# Create tarball
echo "[$(date +%H:%M:%S)] Packaging..."
tar --exclude='__pycache__' --exclude='.git' --exclude='*.pyc' \
    -czf "/tmp/${SERVICE}.tar.gz" -C "$SCRIPT_DIR" .

# Upload
echo "[$(date +%H:%M:%S)] Uploading..."
scp "/tmp/${SERVICE}.tar.gz" "${REMOTE_HOST}:/tmp/"

# Extract and restart
echo "[$(date +%H:%M:%S)] Deploying on remote..."
ssh "$REMOTE_HOST" "
    # Backup .env before anything
    if [ -f ${REMOTE_BASE}/${SERVICE}/deploy/.env ]; then
        cp ${REMOTE_BASE}/${SERVICE}/deploy/.env /tmp/${SERVICE}-env-backup
        echo 'Backed up .env'
    fi

    mkdir -p ${REMOTE_BASE}/${SERVICE}
    tar -xzf /tmp/${SERVICE}.tar.gz -C ${REMOTE_BASE}/${SERVICE}
    rm /tmp/${SERVICE}.tar.gz

    # Restore .env from backup, or create from example
    if [ -f /tmp/${SERVICE}-env-backup ]; then
        cp /tmp/${SERVICE}-env-backup ${REMOTE_BASE}/${SERVICE}/deploy/.env
        echo 'Restored .env from backup'
    elif [ ! -f ${REMOTE_BASE}/${SERVICE}/deploy/.env ]; then
        cp ${REMOTE_BASE}/${SERVICE}/deploy/.env.example ${REMOTE_BASE}/${SERVICE}/deploy/.env
        echo 'Created .env from .env.example - PLEASE CONFIGURE!'
    fi

    cd ${REMOTE_BASE}/${SERVICE}/deploy
    docker compose -p ${SERVICE} down
    docker compose -p ${SERVICE} up -d --build
"

rm "/tmp/${SERVICE}.tar.gz"

echo "[$(date +%H:%M:%S)] Deployed! Testing health..."
sleep 3
ssh "$REMOTE_HOST" "curl -s http://localhost:8910/health"
echo ""
echo "[$(date +%H:%M:%S)] Done!"
