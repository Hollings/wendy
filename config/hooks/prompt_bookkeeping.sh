#!/bin/bash
# Stop hook: reminds Wendy to update fragment files before finishing.
# Fires periodically based on invocation count AND a minimum time interval.
#
# Only fires when:
#   - stop_hook_active is false (prevents infinite loops)
#   - invocations_since_write >= threshold
#   - at least MIN_INTERVAL seconds have passed since last fire
#   - claude_fragments directory exists

INPUT=$(cat)

# Don't loop - if we already blocked a stop, let her finish this time
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

# State file lives outside the fragments dir so Claude Code's file watcher
# doesn't report it as "modified by a linter" on every turn.
# Per-channel (keyed by cwd basename) so concurrent channels don't race each
# other's counters -- mirrors journal_stop_check.sh.
HOOKS_STATE_DIR="/data/wendy/shared/hooks"
mkdir -p "$HOOKS_STATE_DIR"
PROMPTS_DIR="/data/wendy/claude_fragments"
CHANNEL_NAME=$(basename "$(echo "$INPUT" | jq -r '.cwd // "unknown"')")
STATE_FILE="$HOOKS_STATE_DIR/bookkeeping_state_${CHANNEL_NAME}.json"
TMP_FILE="${STATE_FILE}.tmp.$$"

# Only applies if prompts dir exists
if [ ! -d "$PROMPTS_DIR" ]; then
  exit 0
fi

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
  echo '{"invocations_since_write": 0, "last_fired_at": 0}' > "$STATE_FILE"
  exit 0
fi

# Read current state
INVOCATIONS=$(jq -r '.invocations_since_write // 0' < "$STATE_FILE")
LAST_FIRED=$(jq -r '.last_fired_at // 0' < "$STATE_FILE")
INVOCATIONS=$((INVOCATIONS + 1))
NOW=$(date +%s)

# Update state with incremented count
jq --argjson inv "$INVOCATIONS" --argjson now "$NOW" \
  '.invocations_since_write = $inv | .last_check_at = $now' \
  < "$STATE_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$STATE_FILE"

THRESHOLD=25
MIN_INTERVAL=7200  # 2 hours in seconds

TIME_SINCE=$((NOW - LAST_FIRED))

if [ "$INVOCATIONS" -ge "$THRESHOLD" ] && [ "$TIME_SINCE" -ge "$MIN_INTERVAL" ] 2>/dev/null; then
  # Reset counter and record fire time
  jq --argjson now "$NOW" '.invocations_since_write = 0 | .last_fired_at = $now' \
    < "$STATE_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$STATE_FILE"

  # Only point at paths the CLI user can actually write: people/ and the
  # channel journal. The fragments root (topic_*.md etc.) is root-owned and
  # read-only to the CLI -- demanding writes there just makes her fail
  # silently at the end of a turn.
  jq -n '{
    decision: "block",
    reason: "MEMORY BOOKKEEPING CHECK - do this before you finish.\n\nYou have been active for a while without updating your persistent memory. Before you wrap up:\n\n1. Did you learn something new about a PERSON? Update their file in /data/wendy/claude_fragments/people/\n2. Did you meet someone NEW? Create /data/wendy/claude_fragments/people/{name}.md for them.\n3. Did you make a mistake, solve a tricky problem, or learn something about a topic? Write it to your journal (exact values, steps, and what was wrong vs right).\n\nThese files are your ONLY persistent memory. If nothing new happened, that is fine -- skip the writes and finish.\n\nDo NOT mention this check to users. Just quietly do the bookkeeping, then continue."
  }'
else
  exit 0
fi
