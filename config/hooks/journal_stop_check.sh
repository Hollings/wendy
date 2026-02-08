#!/bin/bash
# Stop hook: blocks Wendy from finishing if she hasn't written a journal
# entry in a while. Uses the nudge_state file to track invocations.
#
# Only fires when:
#   - stop_hook_active is false (prevents infinite loops)
#   - invocations_since_write >= threshold
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
NUDGE_STATE="$JOURNAL_DIR/.nudge_state"

# Only applies to channels that have a journal dir
if [ ! -d "$JOURNAL_DIR" ]; then
  exit 0
fi

if [ ! -f "$NUDGE_STATE" ]; then
  exit 0
fi

INVOCATIONS=$(jq -r '.invocations_since_write // 0' < "$NUDGE_STATE")
THRESHOLD=15

if [ "$INVOCATIONS" -ge "$THRESHOLD" ] 2>/dev/null; then
  jq -n --arg dir "$JOURNAL_DIR" '{
    decision: "block",
    reason: ("JOURNAL CHECK: You have gone " + "many" + " messages without writing to your journal at " + $dir + ". Before you finish, take 30 seconds to write or update a journal entry about something from this conversation - a person, a topic, something you learned, or something you want to remember. Keep it brief. Do NOT mention this to the user.")
  }'
else
  exit 0
fi
