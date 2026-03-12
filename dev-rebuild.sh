#!/bin/bash
# Dev deploy helper.
#
# Fast path (code changes only):
#   ./dev-rebuild.sh              -- restart wendy (picks up live source mount instantly)
#   ./dev-rebuild.sh web          -- restart web
#   ./dev-rebuild.sh all          -- restart both
#
# Slow path (deps/Dockerfile changed):
#   ./dev-rebuild.sh --build [wendy|web|all]  -- full image rebuild + recreate

set -e

COMPOSE="docker compose -f deploy/docker-compose.dev.yml"

_container_name() {
    case $1 in
        wendy) echo "wendy-dev" ;;
        web)   echo "wendy-web-dev" ;;
    esac
}

hotreload() {
    local svc=$1
    local cname
    cname=$(_container_name "$svc")
    echo "==> Restarting $svc (live source mount, no rebuild)..."
    docker restart "$cname" 2>/dev/null || $COMPOSE up -d "$svc"
    echo "==> Done. Tailing logs (Ctrl-C to stop)..."
    $COMPOSE logs -f --no-log-prefix "$svc"
}

rebuild() {
    local svc=$1
    echo "==> Building $svc..."
    case $svc in
        wendy) docker build -f deploy/Dockerfile -t deploy-wendy:latest . ;;
        web)   docker build -f services/web/Dockerfile -t deploy-web:latest . ;;
    esac
    echo "==> Force-recreating $svc container..."
    $COMPOSE up -d --force-recreate $svc
    echo "==> Done. Tailing logs (Ctrl-C to stop)..."
    $COMPOSE logs -f --no-log-prefix $svc
}

if [[ "$1" == "--build" ]]; then
    TARGET=${2:-wendy}
    case $TARGET in
        wendy) rebuild wendy ;;
        web)   rebuild web ;;
        all)   rebuild wendy; rebuild web ;;
        *)     echo "Usage: $0 --build [wendy|web|all]"; exit 1 ;;
    esac
else
    TARGET=${1:-wendy}
    case $TARGET in
        wendy) hotreload wendy ;;
        web)   hotreload web ;;
        all)   hotreload wendy; hotreload web ;;
        *)     echo "Usage: $0 [--build] [wendy|web|all]"; exit 1 ;;
    esac
fi
