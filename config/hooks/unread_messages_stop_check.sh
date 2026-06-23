#!/bin/bash
# Stop hook: state-driven unread-message reminder.
#
# Reads QUEUE STATE from SQLite -- it does NOT parse the transcript. If Wendy is
# about to finish her turn while real, non-bot messages remain unread (newer than
# the channel's seen cursor), block ONCE asking her to run `msgs` and respond.
#
# Fires only when:
#   - stop_hook_active is false (prevents infinite loops)
#   - WENDY_CHANNEL_ID is set and the DB exists
#   - there is >= 1 unread real (non-bot, non-command) message
#
# Fails open (exits 0, never blocks) on any missing dependency or error so it can
# never wedge a turn.

INPUT=$(cat)

STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null)
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

CHANNEL_ID="$WENDY_CHANNEL_ID"
# Must be a bare integer; bail otherwise (defensive against an unset/odd value).
if ! [[ "$CHANNEL_ID" =~ ^[0-9]+$ ]]; then
  exit 0
fi

DB="${WENDY_DB_PATH:-/data/wendy/shared/wendy.db}"
if [ ! -f "$DB" ]; then
  exit 0
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  exit 0
fi

BOT_ID="${WENDY_BOT_USER_ID:-0}"
if ! [[ "$BOT_ID" =~ ^[0-9]+$ ]]; then
  BOT_ID=0
fi
SYNTH_THRESHOLD=9000000000000000000

# Count unread real messages: newer than the seen cursor (COALESCE handles a
# channel with no watermark yet -> count everything), below the synthetic
# threshold, not from the bot, and not a bot command (! or - prefix).
COUNT=$(sqlite3 "$DB" "
  SELECT COUNT(*) FROM message_history
  WHERE channel_id = $CHANNEL_ID
    AND message_id < $SYNTH_THRESHOLD
    AND message_id > COALESCE(
      (SELECT last_message_id FROM channel_last_seen WHERE channel_id = $CHANNEL_ID), -1)
    AND author_id != $BOT_ID
    AND (content IS NULL OR (content NOT LIKE '!%' AND content NOT LIKE '-%'));
" 2>/dev/null)

if ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
  exit 0
fi

if [ "$COUNT" -gt 0 ]; then
  jq -n --arg n "$COUNT" '{
    decision: "block",
    reason: ("UNREAD MESSAGES: you have " + $n + " unread message(s) in this channel. Run `msgs` to read them and respond before you finish. Do NOT mention this check to users.")
  }'
else
  exit 0
fi
