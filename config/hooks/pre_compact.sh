#!/bin/bash
# PreCompact hook: writes a flag file so the next nudge prompt tells Wendy
# to restore context via msgs after the compaction.
# Runs with cwd = the CLI project directory (parent channel dir for threads).
#
# The flag is session-scoped: parent channel and thread sessions share this
# cwd, so a bare .compacted couldn't tell whose session was compacted.
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
if [ -n "$SESSION_ID" ]; then
  touch "./.compacted_${SESSION_ID}"
else
  touch ./.compacted
fi
exit 0
