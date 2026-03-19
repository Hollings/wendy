#!/usr/bin/env bash
# Export personal pack from server runtime volume.
#
# Usage: ./scripts/pack-export.sh [server] [output]
#   server  SSH target (default: $DEPLOY_HOST)
#   output  Local output path (default: personal-pack.tar.gz)
#
# Personal pack contents:
#   claude_fragments/people/       - person files (runtime-created)
#   claude_fragments/<id>_*.md     - channel-specific fragments
#   docs/deployment.md             - instance-specific deployment doc

set -euo pipefail

SERVER="${1:-${DEPLOY_HOST:?Set DEPLOY_HOST or pass server as first arg}}"
OUTPUT="${2:-personal-pack.tar.gz}"
CONTAINER="wendy"
REMOTE_TMP="/tmp/wendy-pack.tar.gz"

echo "Exporting personal pack from $SERVER..."

ssh "$SERVER" "
    docker exec $CONTAINER bash -c '
        set -e
        tmpdir=\$(mktemp -d)
        mkdir -p \$tmpdir/claude_fragments \$tmpdir/docs

        if [ -d /data/wendy/claude_fragments/people ]; then
            cp -r /data/wendy/claude_fragments/people \$tmpdir/claude_fragments/
        fi

        find /data/wendy/claude_fragments -maxdepth 1 -name \"[0-9]*\" -type f \
            -exec cp {} \$tmpdir/claude_fragments/ \; 2>/dev/null || true

        if [ -f /data/wendy/docs/deployment.md ]; then
            cp /data/wendy/docs/deployment.md \$tmpdir/docs/
        fi

        tar czf /tmp/wendy-pack.tar.gz -C \$tmpdir .
        rm -rf \$tmpdir
    '
    docker cp $CONTAINER:/tmp/wendy-pack.tar.gz $REMOTE_TMP
    docker exec $CONTAINER rm -f /tmp/wendy-pack.tar.gz
"

scp "$SERVER:$REMOTE_TMP" "$OUTPUT"
ssh "$SERVER" "rm -f $REMOTE_TMP"

echo "Exported to $OUTPUT"
tar tzf "$OUTPUT" | grep -v '^\./\?$' | sed 's/^/  /'
