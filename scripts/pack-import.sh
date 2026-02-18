#!/usr/bin/env bash
# Import personal pack to server runtime volume.
#
# Usage: ./scripts/pack-import.sh [pack] [server]
#   pack    Local pack file (default: personal-pack.tar.gz)
#   server  SSH target (default: ubuntu@100.120.250.100)
#
# Extracts into /data/wendy/ inside the wendy container:
#   claude_fragments/people/   -> /data/wendy/claude_fragments/people/
#   claude_fragments/*.md      -> /data/wendy/claude_fragments/
#   docs/                      -> /data/wendy/docs/

set -euo pipefail

PACK="${1:-personal-pack.tar.gz}"
SERVER="${2:-ubuntu@100.120.250.100}"
CONTAINER="wendy"
REMOTE_TMP="/tmp/wendy-pack.tar.gz"

[ -f "$PACK" ] || { echo "Error: pack file not found: $PACK"; exit 1; }

echo "Importing $PACK to $SERVER..."

scp "$PACK" "$SERVER:$REMOTE_TMP"

ssh "$SERVER" "
    docker cp $REMOTE_TMP $CONTAINER:/tmp/wendy-pack.tar.gz
    docker exec $CONTAINER bash -c '
        set -e
        cd /data/wendy
        mkdir -p docs
        tar xzf /tmp/wendy-pack.tar.gz
        rm -f /tmp/wendy-pack.tar.gz
    '
    rm -f $REMOTE_TMP
"

echo "Import complete."
echo "Restart wendy to pick up new fragments:"
echo "  ssh $SERVER 'cd /srv/wendy-v2/deploy && docker compose restart wendy'"
