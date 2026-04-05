#!/usr/bin/env bash
# sync-fragments.sh - Two-way sync of claude_fragments between repo and live server
#
# Compares repo config/claude_fragments/ with the server's /data/wendy/claude_fragments/.
# Shows diffs and lets you choose which version to keep for each conflict.
#
# Usage:
#   ./scripts/sync-fragments.sh           # Interactive sync
#   ./scripts/sync-fragments.sh --dry-run # Show diffs only, don't change anything
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="${DEPLOY_HOST:?Set DEPLOY_HOST (e.g. export DEPLOY_HOST=user@your-server)}"
LOCAL_DIR="$SCRIPT_DIR/config/claude_fragments"
REMOTE_DIR="/data/wendy/claude_fragments"
CONTAINER="wendy"
TMP_DIR=$(mktemp -d)
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *) echo "Usage: $0 [--dry-run]"; exit 1 ;;
    esac
done

trap 'rm -rf "$TMP_DIR"' EXIT

remote() { ssh -o ConnectTimeout=10 "$SERVER" "$@"; }

# Pull server fragments to temp dir
echo "==> Fetching server fragments..."
remote "docker exec $CONTAINER find $REMOTE_DIR -maxdepth 2 -type f -name '*.md'" > "$TMP_DIR/server_files.txt"

mkdir -p "$TMP_DIR/server"
while IFS= read -r remote_path; do
    rel="${remote_path#$REMOTE_DIR/}"
    mkdir -p "$TMP_DIR/server/$(dirname "$rel")"
    remote "docker exec $CONTAINER cat '$remote_path'" > "$TMP_DIR/server/$rel" 2>/dev/null
done < "$TMP_DIR/server_files.txt"

# Build file lists
find "$LOCAL_DIR" -type f -name '*.md' | while read -r f; do echo "${f#$LOCAL_DIR/}"; done | sort > "$TMP_DIR/local_list.txt"
find "$TMP_DIR/server" -type f -name '*.md' | while read -r f; do echo "${f#$TMP_DIR/server/}"; done | sort > "$TMP_DIR/server_list.txt"

# Categorize files
comm -23 "$TMP_DIR/local_list.txt" "$TMP_DIR/server_list.txt" > "$TMP_DIR/repo_only.txt"
comm -13 "$TMP_DIR/local_list.txt" "$TMP_DIR/server_list.txt" > "$TMP_DIR/server_only.txt"
comm -12 "$TMP_DIR/local_list.txt" "$TMP_DIR/server_list.txt" > "$TMP_DIR/both.txt"

# Find conflicts (files in both that differ)
> "$TMP_DIR/conflicts.txt"
> "$TMP_DIR/identical.txt"
while IFS= read -r rel; do
    if ! diff -q "$LOCAL_DIR/$rel" "$TMP_DIR/server/$rel" > /dev/null 2>&1; then
        echo "$rel" >> "$TMP_DIR/conflicts.txt"
    else
        echo "$rel" >> "$TMP_DIR/identical.txt"
    fi
done < "$TMP_DIR/both.txt"

# Report
REPO_ONLY=$(wc -l < "$TMP_DIR/repo_only.txt" | tr -d ' ')
SERVER_ONLY=$(wc -l < "$TMP_DIR/server_only.txt" | tr -d ' ')
CONFLICTS=$(wc -l < "$TMP_DIR/conflicts.txt" | tr -d ' ')
IDENTICAL=$(wc -l < "$TMP_DIR/identical.txt" | tr -d ' ')

echo ""
echo "=== Fragment Sync Summary ==="
echo "  Identical:    $IDENTICAL"
echo "  Repo only:    $REPO_ONLY (new in repo, not yet on server)"
echo "  Server only:  $SERVER_ONLY (created by Wendy, not in repo)"
echo "  Conflicts:    $CONFLICTS (both sides differ)"
echo ""

# Show repo-only files
if [[ $REPO_ONLY -gt 0 ]]; then
    echo "--- Repo only (will be seeded on next restart) ---"
    cat "$TMP_DIR/repo_only.txt" | sed 's/^/  /'
    echo ""
fi

# Show server-only files
if [[ $SERVER_ONLY -gt 0 ]]; then
    echo "--- Server only (created by Wendy) ---"
    cat "$TMP_DIR/server_only.txt" | sed 's/^/  /'
    echo ""
fi

# Handle conflicts
if [[ $CONFLICTS -eq 0 ]]; then
    echo "No conflicts. Everything is in sync."
    exit 0
fi

echo "--- Conflicts ---"
echo ""

while IFS= read -r rel; do
    echo "================================================================"
    echo "FILE: $rel"
    echo "================================================================"
    diff --color=always -u "$TMP_DIR/server/$rel" "$LOCAL_DIR/$rel" \
        --label "server: $rel" --label "repo: $rel" || true
    echo ""

    if $DRY_RUN; then
        continue
    fi

    while true; do
        read -rp "  [r]epo wins / [s]erver wins / [S]kip ? " choice
        case "$choice" in
            r)
                echo "  -> Pushing repo version to server..."
                scp -q "$LOCAL_DIR/$rel" "$SERVER:/tmp/_frag_sync_$$"
                remote "docker cp /tmp/_frag_sync_$$ $CONTAINER:$REMOTE_DIR/$rel && \
                        docker exec $CONTAINER chown root:root '$REMOTE_DIR/$rel' && \
                        rm /tmp/_frag_sync_$$"
                echo "  Done."
                break
                ;;
            s)
                echo "  -> Pulling server version to repo..."
                cp "$TMP_DIR/server/$rel" "$LOCAL_DIR/$rel"
                echo "  Done. (remember to commit)"
                break
                ;;
            S)
                echo "  -> Skipped."
                break
                ;;
            *)
                echo "  Pick r, s, or S"
                ;;
        esac
    done
    echo ""
done < "$TMP_DIR/conflicts.txt"

echo "==> Sync complete."
