#!/usr/bin/env bash
# deploy.sh - Deploy wendy-v2 to production (Orange Pi)
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
SERVER="${DEPLOY_HOST:-ubuntu@100.120.250.100}"
REMOTE_DIR="/srv/wendy-v2"
COMPOSE="docker compose -f $REMOTE_DIR/deploy/docker-compose.yml"

RESTART_ONLY=false
LOGS_ONLY=false
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

remote() { ssh -o ConnectTimeout=10 "$SERVER" "$@"; }

if $LOGS_ONLY; then
    remote "$COMPOSE logs -f --tail=50 $SERVICES"
    exit 0
fi

if $RESTART_ONLY; then
    echo "==> Restarting $SERVICES..."
    remote "$COMPOSE restart $SERVICES"
    sleep 2
    remote "$COMPOSE logs --tail=15 $SERVICES"
    exit 0
fi

# Full deploy: rsync code then rebuild
echo "==> Uploading to $SERVER:$REMOTE_DIR..."
rsync -az --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='node_modules' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.db' \
    --exclude='*.db-journal' \
    --exclude='*.db-wal' \
    --exclude='wendy_secrets.json' \
    --exclude='personal-pack.tar.gz' \
    --exclude='.claude' \
    "$SCRIPT_DIR/" "$SERVER:$REMOTE_DIR/"

echo "==> Building and restarting $SERVICES..."
remote "cd $REMOTE_DIR/deploy && docker compose up -d --build $SERVICES"

echo "==> Verifying..."
sleep 3
remote "$COMPOSE logs --tail=15 $SERVICES"
echo "==> Deploy complete."
