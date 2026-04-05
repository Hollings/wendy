#!/usr/bin/env bash
# deploy.sh - Deploy wendy-v2 to production (Orange Pi)
#
# Uses tar+scp (works on Windows/Mac/Linux, no rsync needed).
#
# Usage:
#   ./deploy.sh               # Deploy bot only (most common)
#   ./deploy.sh web            # Deploy web service only
#   ./deploy.sh all            # Deploy both
#   ./deploy.sh --restart-only # Restart without uploading/rebuilding
#   ./deploy.sh --logs         # Tail logs
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load DEPLOY_HOST from .env if not already set
if [[ -z "${DEPLOY_HOST:-}" && -f "$SCRIPT_DIR/.env" ]]; then
    DEPLOY_HOST=$(grep '^DEPLOY_HOST=' "$SCRIPT_DIR/.env" | cut -d= -f2-)
fi
SERVER="${DEPLOY_HOST:?Set DEPLOY_HOST in .env or environment}"
REMOTE_DIR="/srv/wendy-v2"
COMPOSE="sudo docker compose -f $REMOTE_DIR/deploy/docker-compose.yml"

RESTART_ONLY=false
LOGS_ONLY=false
BUILD=true
TARGET="wendy"

for arg in "$@"; do
    case "$arg" in
        --restart-only) RESTART_ONLY=true ;;
        --logs)         LOGS_ONLY=true ;;
        wendy|web|all)  TARGET=$arg ;;
        *)              echo "Usage: $0 [--restart-only|--logs] [wendy|web|all]"; exit 1 ;;
    esac
done

case $TARGET in
    wendy) SERVICES="wendy" ;;
    web)   SERVICES="web" ;;
    all)   SERVICES="wendy web" ;;
esac

remote() { ssh -o ConnectTimeout=15 "$SERVER" "$@"; }

# --- Preflight ---
echo "==> Checking SSH to $SERVER..."
remote "echo 'OK'" || { echo "ERROR: Cannot reach $SERVER"; exit 1; }

if $LOGS_ONLY; then
    remote "$COMPOSE logs -f --tail=50 $SERVICES"
    exit 0
fi

if $RESTART_ONLY; then
    echo "==> Restarting $SERVICES..."
    remote "$COMPOSE restart $SERVICES"
    sleep 3
    remote "$COMPOSE logs --tail=15 $SERVICES"
    exit 0
fi

# --- Backup database ---
BACKUP_DIR="/srv/wendy-backups"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

echo "==> Backing up database..."
remote "
    sudo mkdir -p $BACKUP_DIR
    sudo docker run --rm -v wendy_data:/data -v $BACKUP_DIR:/backup alpine \
        cp /data/shared/wendy.db /backup/wendy-${TIMESTAMP}.db 2>/dev/null || true
    sudo find $BACKUP_DIR -name 'wendy-*.db' -mtime +7 -delete 2>/dev/null || true
"

# --- Package ---
echo "==> Packaging..."
TMP_TAR="/tmp/wendy-v2-deploy-$$.tar.gz"
tar -czf "$TMP_TAR" \
    --exclude='node_modules' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.pyc' \
    --exclude='*.egg-info' \
    --exclude='.env' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='*.db' \
    --exclude='*.db-journal' \
    --exclude='*.db-wal' \
    --exclude='.DS_Store' \
    --exclude='wendy_secrets.json' \
    --exclude='personal-pack.tar.gz' \
    --exclude='.claude' \
    -C "$SCRIPT_DIR" .

SIZE=$(du -h "$TMP_TAR" | cut -f1)
echo "    Tarball: $SIZE"

# --- Upload & extract ---
echo "==> Uploading to $SERVER:$REMOTE_DIR..."
scp -o ConnectTimeout=15 "$TMP_TAR" "$SERVER:/tmp/wendy-v2-deploy.tar.gz"
rm -f "$TMP_TAR"

remote "
    sudo rm -rf $REMOTE_DIR
    sudo mkdir -p $REMOTE_DIR
    sudo tar -xzf /tmp/wendy-v2-deploy.tar.gz -C $REMOTE_DIR
    rm -f /tmp/wendy-v2-deploy.tar.gz
"

# --- Build & start ---
echo "==> Building and restarting $SERVICES..."
remote "cd $REMOTE_DIR/deploy && sudo docker compose up -d --build $SERVICES"

# --- Verify ---
echo "==> Verifying..."
sleep 5
remote "$COMPOSE logs --tail=15 $SERVICES"

echo ""
remote "
    if curl -sf http://127.0.0.1:8945/health > /dev/null 2>&1; then
        echo 'wendy API (8945): OK'
    else
        echo 'wendy API (8945): NOT RESPONDING (may still be starting)'
    fi
    if curl -sf http://127.0.0.1:8910/health > /dev/null 2>&1; then
        echo 'wendy-web (8910): OK'
    else
        echo 'wendy-web (8910): NOT RESPONDING (may not be deployed)'
    fi
"
echo "==> Deploy complete."
