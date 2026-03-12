#!/bin/bash
# Stop hook: blocks Wendy from finishing if she hasn't written a journal
# entry in a while. Uses the nudge_state file to track invocations.
#
# Only fires when:
#   - stop_hook_active is false (prevents infinite loops)
#   - invocations_since_write >= threshold
#   - at least MIN_INTERVAL seconds have passed since last fire
#   - journal directory exists (channel has journaling enabled)

INPUT=$(cat)

# Don't loop - if we already blocked a stop, let her finish this time
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

CWD=$(echo "$INPUT" | jq -r '.cwd // empty')
if [ -z "$CWD" ]; then
  exit 0
fi

JOURNAL_DIR="$CWD/journal"

# State file lives outside the channel workspace so Claude Code's file watcher
# doesn't report it as "modified by a linter" on every turn.
CHANNEL_NAME=$(basename "$CWD")
HOOKS_STATE_DIR="/data/wendy/shared/hooks"
mkdir -p "$HOOKS_STATE_DIR"
NUDGE_STATE="$HOOKS_STATE_DIR/journal_nudge_${CHANNEL_NAME}.json"

# Only applies to channels that have a journal dir
if [ ! -d "$JOURNAL_DIR" ]; then
  exit 0
fi

# Initialize state file if missing
if [ ! -f "$NUDGE_STATE" ]; then
  echo '{"invocations_since_write": 0, "last_fired_at": 0}' > "$NUDGE_STATE"
  exit 0
fi

# Read state and increment counter
INVOCATIONS=$(jq -r '.invocations_since_write // 0' < "$NUDGE_STATE")
LAST_FIRED=$(jq -r '.last_fired_at // 0' < "$NUDGE_STATE")
INVOCATIONS=$((INVOCATIONS + 1))
NOW=$(date +%s)

# Update state with incremented count
jq --argjson inv "$INVOCATIONS" --argjson now "$NOW" \
  '.invocations_since_write = $inv | .last_check_at = $now' \
  < "$NUDGE_STATE" > "${NUDGE_STATE}.tmp" && mv "${NUDGE_STATE}.tmp" "$NUDGE_STATE"

THRESHOLD=15
MIN_INTERVAL=10800  # 3 hours in seconds

TIME_SINCE=$((NOW - LAST_FIRED))

if [ "$INVOCATIONS" -ge "$THRESHOLD" ] && [ "$TIME_SINCE" -ge "$MIN_INTERVAL" ] 2>/dev/null; then
  # Reset counter and record fire time
  jq --argjson now "$NOW" '.invocations_since_write = 0 | .last_fired_at = $now' \
    < "$NUDGE_STATE" > "${NUDGE_STATE}.tmp" && mv "${NUDGE_STATE}.tmp" "$NUDGE_STATE"

  jq -n --arg dir "$JOURNAL_DIR" '{
    decision: "block",
    reason: ("JOURNAL CHECK: You have gone many messages without writing to your journal at " + $dir + ". Before you finish, take 30 seconds to write or update a journal entry about something from this conversation - a person, a topic, something you learned, or something you want to remember. Keep it brief. Do NOT mention this to the user.")
  }'
else
  exit 0
fi
