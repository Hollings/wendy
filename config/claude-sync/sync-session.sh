#!/bin/bash
# Claude Code session sync hook for Wendy
# Uploads transcript to central server on session end and before compacts

: "${CLAUDE_SYNC_URL:=https://claude.cee.wtf}"
: "${CLAUDE_SYNC_KEY:=}"
: "${CLAUDE_MACHINE_ID:=wendy-bot}"

# Skip if no API key configured
[ -z "$CLAUDE_SYNC_KEY" ] && exit 0

# Read hook input from stdin
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')

# Validate required fields
[ -z "$SESSION_ID" ] || [ -z "$TRANSCRIPT_PATH" ] && exit 0

# Expand ~ in path
TRANSCRIPT_PATH="${TRANSCRIPT_PATH/#\~/$HOME}"
[ -f "$TRANSCRIPT_PATH" ] || exit 0

# Upload in background (don't block session exit)
curl -s -X POST "$CLAUDE_SYNC_URL/api/sessions" \
    -H "X-API-Key: $CLAUDE_SYNC_KEY" \
    -F "machine_id=$CLAUDE_MACHINE_ID" \
    -F "session_id=$SESSION_ID" \
    -F "transcript=@$TRANSCRIPT_PATH" \
    --max-time 30 >/dev/null 2>&1 &

exit 0
