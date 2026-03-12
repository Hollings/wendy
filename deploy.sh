#!/usr/bin/env bash
# wendy-v2/deploy.sh
#
# Deploy wendy-v2 to Orange Pi, replacing the old wendy-bot.
# Secrets stay in /srv/secrets/wendy/ and are never touched.
#
# Usage:
#   ./deploy.sh               # Full deploy with backup
#   ./deploy.sh --no-backup   # Skip volume backup (faster, riskier)
#   ./deploy.sh --restart-only # Restart containers without re-uploading code
#   ./deploy.sh --dry-run     # Show what would happen
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="${DEPLOY_HOST:-ubuntu@100.120.250.100}"
SERVICE="wendy-v2"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="~/wendy-backup-${TIMESTAMP}"

# === Flags ===
NO_BACKUP=false
RESTART_ONLY=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --no-backup)    NO_BACKUP=true ;;
        --restart-only) RESTART_ONLY=true ;;
        --dry-run)      DRY_RUN=true ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

# === Helpers ===
info()    { echo "[$(date +%H:%M:%S)] $*"; }
success() { echo "[$(date +%H:%M:%S)] OK: $*"; }
warn()    { echo "[$(date +%H:%M:%S)] WARN: $*" >&2; }
error()   { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; exit 1; }

remote() { ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no "$REMOTE_HOST" "$@"; }
upload()  { scp -o ConnectTimeout=15 -o StrictHostKeyChecking=no "$1" "$REMOTE_HOST:$2"; }

if $DRY_RUN; then
    info "DRY RUN mode — no changes will be made"
fi

# === Phase 1: Preflight ===
info "=== Phase 1: Preflight ==="

info "Checking SSH connectivity to $REMOTE_HOST..."
remote "echo 'SSH OK'" || error "Cannot reach $REMOTE_HOST"

info "Checking secrets on server..."
remote "[ -f /srv/secrets/wendy/bot.env ] || { echo 'ERROR: bot.env missing'; exit 1; }" || \
    error "/srv/secrets/wendy/bot.env not found on server. Run setup-secrets.sh first."
remote "[ -f /srv/secrets/wendy/sites.env ] || { echo 'ERROR: sites.env missing'; exit 1; }" || \
    error "/srv/secrets/wendy/sites.env not found on server."

success "Preflight passed"

# === Phase 2: Backup ===
info "=== Phase 2: Backup ==="

if $NO_BACKUP; then
    warn "Skipping backup (--no-backup)"
elif $DRY_RUN; then
    info "[DRY RUN] Would backup to $BACKUP_DIR on server"
else
    info "Creating backup at $BACKUP_DIR on server..."
    remote "
        set -e
        mkdir -p $BACKUP_DIR

        # Back up old wendy-bot code and config
        if [ -d /srv/wendy-bot ]; then
            echo 'Backing up /srv/wendy-bot/ ...'
            cp -r /srv/wendy-bot $BACKUP_DIR/wendy-bot-code
            echo 'Code backed up.'
        else
            echo 'Note: /srv/wendy-bot not found, skipping code backup'
        fi

        # Back up wendy_data volume (SQLite DB, channels, fragments)
        if docker volume inspect wendy_data > /dev/null 2>&1; then
            echo 'Backing up wendy_data volume...'
            docker run --rm \
                -v wendy_data:/data \
                -v $BACKUP_DIR:/backup \
                alpine tar czf /backup/wendy_data.tar.gz -C /data .
            echo 'wendy_data backed up.'
        else
            echo 'Note: wendy_data volume not found, skipping'
        fi

        # Back up claude_config volume (session JSONL files)
        if docker volume inspect claude_config > /dev/null 2>&1; then
            echo 'Backing up claude_config volume...'
            docker run --rm \
                -v claude_config:/data \
                -v $BACKUP_DIR:/backup \
                alpine tar czf /backup/claude_config.tar.gz -C /data .
            echo 'claude_config backed up.'
        else
            echo 'Note: claude_config volume not found, skipping'
        fi

        echo ''
        echo 'Backup contents:'
        ls -lh $BACKUP_DIR
    "
    success "Backup saved to $BACKUP_DIR on server"
fi

# === Phase 3: Stop Old Containers ===
info "=== Phase 3: Stop Old Containers ==="

if $DRY_RUN; then
    info "[DRY RUN] Would stop old wendy-bot, wendy-sites, wendy-games containers"
else
    remote "
        # Stop old wendy-bot (3-process architecture: bot, proxy, orchestrator)
        if [ -f /srv/wendy-bot/deploy/docker-compose.yml ]; then
            echo 'Stopping old wendy-bot containers...'
            cd /srv/wendy-bot/deploy
            docker compose -p wendy-bot down 2>/dev/null || true
        fi

        # Stop old wendy-sites if deployed separately
        if [ -f /srv/wendy-sites/deploy/docker-compose.yml ]; then
            echo 'Stopping wendy-sites containers...'
            cd /srv/wendy-sites/deploy
            docker compose down 2>/dev/null || true
        fi

        # Stop old wendy-games if deployed separately
        if [ -f /srv/wendy-games/deploy/docker-compose.yml ]; then
            echo 'Stopping wendy-games containers...'
            cd /srv/wendy-games/deploy
            docker compose down 2>/dev/null || true
        fi

        # Stop any remaining wendy bot/proxy/orchestrator/sites containers
        # (but NOT wendy-game-* containers — those stay up)
        for cname in wendy-bot-bot wendy-bot-proxy wendy-bot-orchestrator wendy-sites-web wendy-web; do
            if docker ps -q --filter name=\$cname | grep -q .; then
                echo \"Stopping \$cname...\"
                docker stop \$(docker ps -q --filter name=\$cname) 2>/dev/null || true
            fi
        done

        echo 'Old containers stopped.'
    "
    success "Old containers stopped"
fi

if $RESTART_ONLY; then
    # === Restart Only Path ===
    info "=== Restart Only: Rebuilding and restarting containers ==="
    if $DRY_RUN; then
        info "[DRY RUN] Would restart wendy-v2 containers"
    else
        remote "
            [ -d /srv/wendy-v2/deploy ] || { echo 'ERROR: /srv/wendy-v2 not deployed yet. Run without --restart-only first.'; exit 1; }
            cd /srv/wendy-v2/deploy
            docker compose -p wendy-v2 up -d --build --force-recreate --remove-orphans
        "
        success "Containers restarted"
    fi
else
    # === Phase 4: Package ===
    info "=== Phase 4: Package wendy-v2 ==="

    if $DRY_RUN; then
        info "[DRY RUN] Would package $SCRIPT_DIR to /tmp/wendy-v2.tar.gz"
    else
        info "Creating tarball..."
        tar \
            --exclude='node_modules' \
            --exclude='.git' \
            --exclude='__pycache__' \
            --exclude='.pytest_cache' \
            --exclude='*.pyc' \
            --exclude='*.egg-info' \
            --exclude='.env' \
            --exclude='.DS_Store' \
            -czf /tmp/wendy-v2.tar.gz \
            -C "$SCRIPT_DIR" .
        SIZE="$(du -h /tmp/wendy-v2.tar.gz | cut -f1)"
        success "Packaged wendy-v2 ($SIZE)"
    fi

    # === Phase 5: Upload & Extract ===
    info "=== Phase 5: Upload & Extract ==="

    if $DRY_RUN; then
        info "[DRY RUN] Would upload and extract to /srv/wendy-v2/"
    else
        info "Uploading to $REMOTE_HOST..."
        upload /tmp/wendy-v2.tar.gz /tmp/

        info "Extracting on server..."
        remote "
            rm -rf /srv/wendy-v2
            mkdir -p /srv/wendy-v2
            tar -xzf /tmp/wendy-v2.tar.gz -C /srv/wendy-v2
            rm /tmp/wendy-v2.tar.gz
            echo 'Extracted to /srv/wendy-v2/'
        "

        rm -f /tmp/wendy-v2.tar.gz
        success "Uploaded and extracted"
    fi

    # === Phase 6: Ensure Prerequisites ===
    info "=== Phase 6: Ensure Prerequisites ==="

    if $DRY_RUN; then
        info "[DRY RUN] Would ensure volumes and directories exist"
    else
        remote "
            # Create external Docker volumes if they don't exist yet
            docker volume create wendy_data 2>/dev/null && echo 'Created wendy_data volume' || echo 'wendy_data volume already exists'
            docker volume create claude_config 2>/dev/null && echo 'Created claude_config volume' || echo 'claude_config volume already exists'
            docker volume create wendy-sites_sites_data 2>/dev/null && echo 'Created wendy-sites_sites_data volume' || echo 'wendy-sites_sites_data volume already exists'

            # Shared data directories inside the volume
            docker run --rm -v wendy_data:/data alpine sh -c '
                mkdir -p /data/shared /data/channels /data/claude_fragments/people
            '

            # Games bind-mount directory on host
            mkdir -p /srv/wendy-games/data
            echo 'Prerequisites ready.'
        "
        success "Prerequisites ensured"
    fi

    # === Phase 7: Start wendy-v2 ===
    info "=== Phase 7: Start wendy-v2 ==="

    if $DRY_RUN; then
        info "[DRY RUN] Would run: docker compose -p wendy-v2 up -d --build --remove-orphans"
    else
        info "Building images and starting containers (this may take a while)..."
        remote "
            cd /srv/wendy-v2/deploy
            docker compose -p wendy-v2 up -d --build --remove-orphans
        "
        success "wendy-v2 containers started"
    fi
fi

# === Phase 8: Verify ===
info "=== Phase 8: Verify ==="

if $DRY_RUN; then
    info "[DRY RUN] Would check container status and health endpoints"
else
    info "Waiting for containers to initialize..."
    sleep 10

    echo ""
    echo "--- Container Status ---"
    remote "docker ps --filter name=wendy --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

    echo ""
    echo "--- Health Checks ---"
    remote "
        if curl -sf http://127.0.0.1:8945/health > /dev/null 2>&1; then
            echo 'wendy bot API (8945): OK'
        else
            echo 'wendy bot API (8945): NOT RESPONDING'
        fi

        if curl -sf http://127.0.0.1:8910/health > /dev/null 2>&1; then
            echo 'wendy-web (8910):     OK'
        else
            echo 'wendy-web (8910):     NOT RESPONDING'
        fi
    "
fi

# === Done ===
echo ""
echo "========================================"
info "Deploy complete!"
echo "========================================"
echo ""
if ! $DRY_RUN && ! $NO_BACKUP; then
    echo "Backup location on server: $BACKUP_DIR"
    echo ""
fi
echo "Next steps:"
echo "  1. Check Discord — Wendy should respond in configured channels"
echo "  2. Check https://wendy.monster for the brain feed"
echo "  3. If Claude CLI needs re-auth:"
echo "     ssh $REMOTE_HOST 'docker exec -it wendy claude login'"
echo "  4. View logs:"
echo "     ssh $REMOTE_HOST 'docker logs -f wendy'"
echo "     ssh $REMOTE_HOST 'docker logs -f wendy-web'"
echo ""
